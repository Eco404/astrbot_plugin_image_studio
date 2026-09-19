from __future__ import annotations

import importlib.util
import os
import subprocess
import sys

import pytest
from astrbot_plugin_image_studio.tests.support.paths import PLUGIN_ROOT

SCRIPT = PLUGIN_ROOT / "scripts" / "verify.py"
SPEC = importlib.util.spec_from_file_location("image_studio_verify", SCRIPT)
verify = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(verify)


def test_suite_selection_rejects_paths_and_incompatible_browser():
    assert verify.selected_suites(["hash", "webui_hash.cjs"], None) == [
        SCRIPT.parents[1] / "tests" / "webui" / "webui_hash.cjs"
    ]
    with pytest.raises(verify.VerificationError, match="Unknown WebUI"):
        verify.selected_suites(["../../untrusted.cjs"], None)
    with pytest.raises(verify.VerificationError, match="fixed browser matrix"):
        verify.selected_suites(["detail_instant"], "chromium")
    with pytest.raises(verify.VerificationError, match="fixed browser matrix"):
        verify.selected_suites(["browser"], "webkit")
    assert verify.selected_suites(["comfy_workspace"], "webkit")


def test_nested_suites_keep_existing_short_names(monkeypatch, tmp_path):
    suite = tmp_path / "tests" / "webui" / "gallery" / "webui_gallery.cjs"
    suite.parent.mkdir(parents=True)
    suite.write_text("// node-only fixture", encoding="utf-8")
    monkeypatch.setattr(verify, "ROOT", tmp_path)
    assert verify.selected_suites(["gallery", "webui_gallery.cjs"], None) == [suite]


def test_duplicate_suite_basenames_fail_with_both_paths(monkeypatch, tmp_path):
    for folder in ("gallery", "settings"):
        suite = tmp_path / "tests" / "webui" / folder / "webui_same.cjs"
        suite.parent.mkdir(parents=True)
        suite.write_text("// node-only fixture", encoding="utf-8")
    monkeypatch.setattr(verify, "ROOT", tmp_path)
    with pytest.raises(
        verify.VerificationError, match="Duplicate WebUI suite name"
    ) as exc:
        verify.suites()
    assert "gallery/webui_same.cjs" in str(exc.value)
    assert "settings/webui_same.cjs" in str(exc.value)


def test_child_environment_clears_external_url_and_uses_current_python(
    monkeypatch, tmp_path
):
    host = tmp_path / "host"
    (host / "astrbot").mkdir(parents=True)
    monkeypatch.setenv("STUDIO_TEST_URL", "https://deployed.invalid")
    monkeypatch.setenv("STUDIO_BROWSER", "webkit")
    monkeypatch.setenv("STUDIO_PLAYWRIGHT", "/tmp/old-library")
    monkeypatch.setenv("PYTHONPATH", "keep-this-search-path")

    env = verify.child_environment(host)

    assert "STUDIO_TEST_URL" not in env
    assert "STUDIO_BROWSER" not in env
    assert "STUDIO_PLAYWRIGHT" not in env
    assert env["STUDIO_PYTHON"] == sys.executable
    assert str(host) in env["PYTHONPATH"].split(os.pathsep)
    assert "keep-this-search-path" in env["PYTHONPATH"].split(os.pathsep)


def test_playwright_requires_repository_pinned_dependency(monkeypatch, tmp_path):
    monkeypatch.setattr(verify, "ROOT", tmp_path)
    with pytest.raises(verify.VerificationError, match="npm ci"):
        verify.playwright_module()
    (tmp_path / "package.json").write_text(
        '{"devDependencies":{"playwright":"1.62.0"}}'
    )
    module = tmp_path / "node_modules" / "playwright"
    module.mkdir(parents=True)
    (module / "package.json").write_text('{"version":"1.60.0"}')
    with pytest.raises(verify.VerificationError, match="differs from pinned"):
        verify.playwright_module()
    (module / "package.json").write_text('{"version":"1.62.0"}')
    assert verify.playwright_module() == module


def test_run_timeout_and_error_stop_owned_subprocesses(monkeypatch, tmp_path):
    processes = []
    real_popen = subprocess.Popen

    def remember(*args, **kwargs):
        process = real_popen(*args, **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr(verify.subprocess, "Popen", remember)
    with pytest.raises(verify.VerificationError, match="timed out"):
        verify.run(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            cwd=tmp_path,
            env=os.environ.copy(),
            timeout=0.1,
        )
    with pytest.raises(verify.VerificationError, match="exit code 7"):
        verify.run(
            [sys.executable, "-c", "raise SystemExit(7)"],
            cwd=tmp_path,
            env=os.environ.copy(),
            timeout=3,
        )
    assert len(processes) == 2
    assert all(process.poll() is not None for process in processes)


@pytest.mark.parametrize("failure", [False, True])
def test_harness_is_owned_and_stopped_after_success_or_suite_failure(
    monkeypatch, tmp_path, failure
):
    root = tmp_path / "repo"
    (root / "tests" / "support").mkdir(parents=True)
    (root / "tests" / "support" / "webui_harness.py").write_text(
        "import argparse\nfrom http.server import BaseHTTPRequestHandler,HTTPServer\n"
        "parser=argparse.ArgumentParser();parser.add_argument('--port',type=int);args=parser.parse_args()\n"
        "class Handler(BaseHTTPRequestHandler):\n"
        " def do_GET(self):\n"
        "  self.send_response(200);self.end_headers();self.wfile.write(b'<iframe id=\"studio\">')\n"
        "HTTPServer(('127.0.0.1',args.port),Handler).serve_forever()\n"
    )
    monkeypatch.setattr(verify, "ROOT", root)
    work = tmp_path / "isolated"
    work.mkdir()
    processes = []
    real_popen = subprocess.Popen

    def remember(*args, **kwargs):
        assert kwargs["cwd"] == work
        assert kwargs["env"]["TMPDIR"] == str(work)
        process = real_popen(*args, **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr(verify.subprocess, "Popen", remember)
    try:
        with verify.isolated_harness(
            env=os.environ.copy(), workdir=work, log=tmp_path / "harness.log", timeout=5
        ) as url:
            assert url.startswith("http://127.0.0.1:")
            assert processes[0].poll() is None
            if failure:
                raise RuntimeError("simulated browser assertion failure")
    except RuntimeError as exc:
        assert failure and "simulated browser" in str(exc)
    assert len(processes) == 1 and processes[0].poll() is not None


def test_harness_startup_failure_is_bounded_and_reported(monkeypatch, tmp_path):
    (tmp_path / "tests" / "support").mkdir(parents=True)
    (tmp_path / "tests" / "support" / "webui_harness.py").write_text(
        "raise SystemExit(13)\n"
    )
    monkeypatch.setattr(verify, "ROOT", tmp_path)
    with pytest.raises(verify.VerificationError, match="exited with code 13"):
        with verify.isolated_harness(
            env=os.environ.copy(),
            workdir=tmp_path,
            log=tmp_path / "harness.log",
            timeout=1,
        ):
            pytest.fail("Failed harness must not run a browser suite")


@pytest.mark.parametrize("value", ["0", "-1", "nan", "inf", "text"])
def test_timeout_arguments_are_finite_and_positive(value):
    with pytest.raises(SystemExit):
        verify.parser().parse_args(["--timeout", value])
