#!/usr/bin/env python3
"""Run reproducible local checks without using deployment data or services."""

from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BROWSER_SELECTORS = ("STUDIO_BROWSER", "STUDIO_BROWSERS", "STUDIO_ENGINES")


class VerificationError(RuntimeError):
    pass


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--backend", action="store_true", help="run Python tests")
    result.add_argument(
        "--static", action="store_true", help="check Ruff, JS syntax and Git whitespace"
    )
    result.add_argument(
        "--package", action="store_true", help="build and validate the installation ZIP"
    )
    result.add_argument(
        "--webui",
        nargs="+",
        action="extend",
        default=[],
        metavar="SUITE",
        help="run named isolated browser suites; use --list-webui",
    )
    result.add_argument(
        "--browser",
        choices=("chromium", "webkit"),
        help="override browser for compatible suites; omitted uses each suite's own matrix",
    )
    result.add_argument(
        "--list-webui",
        action="store_true",
        help="list browser suites and engine-selection support",
    )
    result.add_argument(
        "--astrbot-root",
        type=Path,
        default=None,
        help="AstrBot source checkout (also ASTRBOT_ROOT); defaults to sibling AstrBot",
    )
    result.add_argument(
        "--timeout",
        type=positive_number,
        default=600,
        help="seconds per check/browser suite (default 600)",
    )
    result.add_argument(
        "--startup-timeout",
        type=positive_number,
        default=60,
        help="seconds for each isolated harness to become ready",
    )
    result.add_argument(
        "--artifacts-dir",
        type=Path,
        default=ROOT / "dist" / "verification",
        help="browser screenshots and harness logs (default dist/verification)",
    )
    return result


def positive_number(value: str) -> float:
    try:
        number = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a positive number") from exc
    if not 0 < number < float("inf"):
        raise argparse.ArgumentTypeError("must be a finite positive number")
    return number


def suites() -> dict[str, Path]:
    return {
        path.stem.removeprefix("webui_"): path
        for path in sorted((ROOT / "tests").glob("webui_*.cjs"))
    }


def browser_support(path: Path) -> str:
    source = path.read_text(encoding="utf-8")
    if "STUDIO_PLAYWRIGHT" not in source:
        return "node"
    if any(f"process.env.{selector}" in source for selector in BROWSER_SELECTORS):
        return "selectable"
    if "webkit" not in source:
        return "chromium"
    return "native-matrix"


def selected_suites(names: list[str], browser: str | None) -> list[Path]:
    available = suites()
    result: list[Path] = []
    for name in names:
        key = name.removeprefix("webui_").removesuffix(".cjs")
        if key not in available:
            raise VerificationError(f"Unknown WebUI suite: {name}. Use --list-webui.")
        path = available[key]
        support = browser_support(path)
        if browser and (
            support == "native-matrix"
            or (support == "chromium" and browser != "chromium")
        ):
            raise VerificationError(
                f"{key} has a fixed browser matrix; omit --browser to run it as written."
            )
        if path not in result:
            result.append(path)
    return result


def child_environment(astrbot_root: Path | None) -> dict[str, str]:
    env = os.environ.copy()
    host = astrbot_root or Path(env.get("ASTRBOT_ROOT", str(ROOT.parent / "AstrBot")))
    paths = [str(ROOT.parent)]
    if (host / "astrbot").is_dir():
        paths.append(str(host.resolve()))
    if env.get("PYTHONPATH"):
        paths.append(env["PYTHONPATH"])
    env["PYTHONPATH"] = os.pathsep.join(paths)
    env["PYTHONUNBUFFERED"] = "1"
    env["STUDIO_PYTHON"] = sys.executable
    # Never inherit a URL or browser selector aimed at some other server/run.
    for name in (
        "STUDIO_TEST_URL",
        "STUDIO_PLAYWRIGHT",
        *BROWSER_SELECTORS,
        "STUDIO_DETAIL_CASES",
        "STUDIO_DETAIL_SCENARIO",
    ):
        env.pop(name, None)
    return env


def stop_process(process: subprocess.Popen) -> None:
    """Stop the entire owned process group, including browser descendants."""
    if os.name == "posix":
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    elif process.poll() is None:
        process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        if os.name == "posix":
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        else:
            process.kill()
        process.wait(timeout=5)
    finally:
        # A shell or Node process can exit before an owned descendant does.
        if os.name == "posix":
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


def run(
    command: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
    timeout: float,
    label: str | None = None,
) -> None:
    print(f"\n$ {label or shlex.join(command)}", flush=True)
    process = subprocess.Popen(
        command, cwd=cwd, env=env, start_new_session=os.name == "posix"
    )
    try:
        code = process.wait(timeout=timeout)
        if code:
            raise VerificationError(
                f"Command failed with exit code {code}: {shlex.join(command)}"
            )
    except subprocess.TimeoutExpired as exc:
        raise VerificationError(
            f"Check timed out after {timeout:g}s: {shlex.join(command)}"
        ) from exc
    finally:
        stop_process(process)


def require_program(name: str) -> str:
    executable = shutil.which(name)
    if not executable:
        raise VerificationError(
            f"Missing {name} on PATH. Install it before running this check; Node.js >= 20 is needed for WebUI checks."
        )
    return executable


def require_python(
    modules: list[str], *, cwd: Path, env: dict[str, str], timeout: float
) -> None:
    script = "import importlib.util,sys; missing=[name for name in sys.argv[1:] if importlib.util.find_spec(name) is None]; print('Missing Python modules: '+', '.join(missing)) if missing else None; sys.exit(bool(missing))"
    try:
        run([sys.executable, "-c", script, *modules], cwd=cwd, env=env, timeout=timeout)
    except VerificationError as exc:
        raise VerificationError(
            f"{exc}\nUse the activated virtual environment; install AstrBot dependencies and the development requirements in docs/TESTING.md. For a source checkout, pass --astrbot-root or set ASTRBOT_ROOT."
        ) from exc


def tracked_source_files() -> tuple[list[str], list[str]]:
    require_program("git")
    output = subprocess.check_output(
        [
            "git",
            "ls-files",
            "--cached",
            "--others",
            "--exclude-standard",
            "-z",
            "*.py",
            "*.js",
            "*.cjs",
        ],
        cwd=ROOT,
    )
    paths = sorted({Path(os.fsdecode(value)) for value in output.split(b"\0") if value})
    python, javascript = [], []
    for path in paths:
        if "vendor" in path.parts or not (ROOT / path).is_file():
            continue
        (python if path.suffix == ".py" else javascript).append(str(path))
    return python, javascript


def static_checks(*, env: dict[str, str], timeout: float) -> None:
    node = require_program("node")
    require_python(["ruff"], cwd=ROOT, env=env, timeout=timeout)
    python, javascript = tracked_source_files()
    run(
        [sys.executable, "-m", "ruff", "check", "--select", "F,E9", *python],
        cwd=ROOT,
        env=env,
        timeout=timeout,
        label=f"python -m ruff check --select F,E9 ({len(python)} Python files)",
    )
    run(
        [sys.executable, "-m", "ruff", "format", "--check", *python],
        cwd=ROOT,
        env=env,
        timeout=timeout,
        label=f"python -m ruff format --check ({len(python)} Python files)",
    )
    print(
        f"\nChecking JavaScript syntax: {len(javascript)} first-party files", flush=True
    )
    # One Node process keeps a large suite list quiet; each syntax check is still bounded.
    script = "const {spawnSync}=require('node:child_process');for(const file of process.argv.slice(1)){const r=spawnSync(process.execPath,['--check',file],{stdio:'inherit',timeout:30000});if(r.error||r.status!==0){console.error('Syntax check failed:',file,r.error||'');process.exit(1)}}"
    run(
        [node, "-e", script, *javascript],
        cwd=ROOT,
        env=env,
        timeout=timeout,
        label=f"node --check ({len(javascript)} JavaScript files)",
    )
    run(["git", "diff", "--check"], cwd=ROOT, env=env, timeout=timeout)
    run(["git", "diff", "--cached", "--check"], cwd=ROOT, env=env, timeout=timeout)


def playwright_module() -> Path:
    module = ROOT / "node_modules" / "playwright"
    package = ROOT / "package.json"
    try:
        expected = json.loads(package.read_text(encoding="utf-8"))["devDependencies"][
            "playwright"
        ]
        installed = json.loads((module / "package.json").read_text(encoding="utf-8"))[
            "version"
        ]
    except (OSError, KeyError, ValueError) as exc:
        raise VerificationError(
            "Playwright is not installed for this repository. Run npm ci, then npx playwright install chromium webkit. The runner does not install dependencies."
        ) from exc
    if installed != expected:
        raise VerificationError(
            f"Playwright version {installed} differs from pinned {expected}; run npm ci."
        )
    return module.resolve()


def free_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def wait_ready(process: subprocess.Popen, url: str, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise VerificationError(
                f"Isolated harness exited with code {process.returncode} before it was ready."
            )
        try:
            with opener.open(
                url, timeout=min(1, max(0.01, deadline - time.monotonic()))
            ) as response:
                if b'<iframe id="studio"' in response.read(65536):
                    return
        except (OSError, urllib.error.URLError):
            pass
        time.sleep(0.1)
    raise VerificationError(
        f"Isolated harness did not become ready within {timeout:g}s."
    )


@contextmanager
def isolated_harness(*, env: dict[str, str], workdir: Path, log: Path, timeout: float):
    url = f"http://127.0.0.1:{free_port()}"
    port = url.rsplit(":", 1)[1]
    harness_env = {
        **env,
        "TMPDIR": str(workdir),
        "TMP": str(workdir),
        "TEMP": str(workdir),
    }
    with log.open("w", encoding="utf-8") as output:
        process = subprocess.Popen(
            [sys.executable, str(ROOT / "tests" / "webui_harness.py"), "--port", port],
            cwd=workdir,
            env=harness_env,
            stdout=output,
            stderr=subprocess.STDOUT,
            start_new_session=os.name == "posix",
        )
        try:
            wait_ready(process, url, timeout)
            yield url
        except BaseException:
            output.flush()
            print(
                f"\nHarness log: {log}\n{log.read_text(encoding='utf-8', errors='replace')[-8000:]}",
                file=sys.stderr,
            )
            raise
        finally:
            stop_process(process)


def webui_checks(
    paths: list[Path], args: argparse.Namespace, *, env: dict[str, str], workdir: Path
) -> None:
    node = require_program("node")
    needs_browser = any(browser_support(path) != "node" for path in paths)
    module = playwright_module() if needs_browser else None
    if needs_browser:
        require_python(
            ["astrbot", "fastapi", "uvicorn", "PIL", "yaml"],
            cwd=workdir,
            env=env,
            timeout=args.timeout,
        )
    args.artifacts_dir.mkdir(parents=True, exist_ok=True)
    for path in paths:
        artifacts = Path(
            tempfile.mkdtemp(prefix=f"{path.stem}-", dir=args.artifacts_dir.resolve())
        )
        browser_env = {
            **env,
            "TMPDIR": str(artifacts),
            "TMP": str(artifacts),
            "TEMP": str(artifacts),
        }
        if module:
            browser_env["STUDIO_PLAYWRIGHT"] = str(module)
        if args.browser:
            browser_env.update({name: args.browser for name in BROWSER_SELECTORS})
        print(f"\nWebUI suite: {path.stem}; artifacts: {artifacts}", flush=True)
        with tempfile.TemporaryDirectory(
            prefix=f"{path.stem}-", dir=workdir
        ) as isolated:
            isolated_path = Path(isolated)
            if browser_support(path) == "node":
                run(
                    [node, str(path)],
                    cwd=isolated_path,
                    env=browser_env,
                    timeout=args.timeout,
                )
                continue
            with isolated_harness(
                env=env,
                workdir=isolated_path,
                log=artifacts / "harness.log",
                timeout=args.startup_timeout,
            ) as url:
                browser_env["STUDIO_TEST_URL"] = url
                try:
                    run(
                        [node, str(path)],
                        cwd=isolated_path,
                        env=browser_env,
                        timeout=args.timeout,
                    )
                except VerificationError as exc:
                    raise VerificationError(
                        f"{exc}\nIf browser binaries or OS libraries are missing, run npx playwright install --with-deps chromium webkit. Artifacts: {artifacts}"
                    ) from exc


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.list_webui:
            for name, path in suites().items():
                print(f"{name:32} {browser_support(path)}")
            return 0
        if args.browser and not args.webui:
            raise VerificationError("--browser requires --webui SUITE.")
        paths = selected_suites(args.webui, args.browser)
        if not any((args.backend, args.static, args.package, paths)):
            args.backend = args.static = args.package = True
        env = child_environment(args.astrbot_root)
        with tempfile.TemporaryDirectory(prefix="image-studio-verify-") as temporary:
            workdir = Path(temporary)
            if args.static:
                static_checks(env=env, timeout=args.timeout)
            if args.backend:
                require_python(
                    ["pytest", "astrbot"], cwd=workdir, env=env, timeout=args.timeout
                )
                run(
                    [sys.executable, "-m", "pytest", "-q", str(ROOT / "tests")],
                    cwd=workdir,
                    env=env,
                    timeout=args.timeout,
                )
            if paths:
                webui_checks(paths, args, env=env, workdir=workdir)
            if args.package:
                require_python(["yaml"], cwd=workdir, env=env, timeout=args.timeout)
                run(
                    [
                        sys.executable,
                        "-B",
                        str(ROOT / "scripts" / "build_plugin_package.py"),
                    ],
                    cwd=ROOT,
                    env=env,
                    timeout=args.timeout,
                )
        print("\nSelected verification checks passed.", flush=True)
        return 0
    except VerificationError as exc:
        print(f"\nVerification failed: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print(
            "\nVerification interrupted; owned subprocesses stopped.", file=sys.stderr
        )
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
