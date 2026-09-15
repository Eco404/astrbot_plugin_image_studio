#!/usr/bin/env python3
"""Build a reproducible AstrBot installation ZIP from the working tree."""

from __future__ import annotations

import argparse
import ast
import hashlib
import os
import re
import tempfile
import zipfile
from pathlib import Path, PurePosixPath

import yaml

RUNTIME_FILES = (
    "__init__.py",
    "main.py",
    "appearance.py",
    "config.py",
    "comfyui.py",
    "comfyui_workflows.py",
    "comfyui_jobs.py",
    "comfyui_runtime.py",
    "comfyui_support.py",
    "database_schema.py",
    "external_gallery.py",
    "external_timestamps.py",
    "gallery_preferences.py",
    "image_metadata.py",
    "models.py",
    "novelai.py",
    "novelai_catalog.py",
    "novelai_inputs.py",
    "novelai_inpaint.py",
    "parameter_exchange.py",
    "providers.py",
    "service.py",
    "storage.py",
    "metadata.yaml",
    "_conf_schema.json",
    "requirements.txt",
    "README.md",
    "docs/images/generate.png",
)
OPTIONAL_FILES = ("LICENSE", "logo.png", "CHANGELOG.md")
RUNTIME_DIRECTORIES = ("pages",)
REQUIRED_ARCHIVE_FILES = RUNTIME_FILES + (
    "pages/image-studio/index.html",
    "pages/image-studio/app.js",
    "pages/image-studio/app.css",
    "pages/image-studio/novelai-controls.js",
    "pages/image-studio/novelai-controls.css",
    "pages/image-studio/comfyui-controls.js",
    "pages/image-studio/comfyui-controls.css",
    "pages/image-studio/tooltip.js",
    "pages/image-studio/tooltip.css",
    "pages/image-studio/appearance.js",
    "pages/image-studio/appearance.css",
    "pages/image-studio/controls.css",
    "pages/image-studio/library.js",
    "pages/image-studio/dialog-motion.js",
    "pages/image-studio/external-sources.js",
    "pages/image-studio/gallery-preferences.js",
    "pages/image-studio/hash.js",
    "pages/image-studio/backdrop.js",
    "pages/image-studio/viewer-backdrop.js",
    "pages/image-studio/detail-swipe.js",
    "pages/image-studio/image-placeholder.js",
    "pages/image-studio/detail-swipe.css",
    "pages/image-studio/library.css",
    "pages/image-studio/select.js",
    "pages/image-studio/select.css",
    "pages/image-studio/sortable.js",
    "pages/image-studio/sortable.css",
    "pages/image-studio/vendor/photoswipe/photoswipe.umd.min.js",
    "pages/image-studio/vendor/photoswipe/photoswipe.css",
    "pages/image-studio/vendor/photoswipe/LICENSE",
    "pages/image-studio/vendor/exifreader/exif-reader.js",
    "pages/image-studio/vendor/exifreader/LICENSE",
    "pages/image-studio/vendor/lucide/icons.js",
    "pages/image-studio/vendor/lucide/LICENSE",
    "pages/image-studio/vendor/js-sha256/sha256.min.js",
    "pages/image-studio/vendor/js-sha256/LICENSE",
)
IGNORED_NAMES = {
    ".DS_Store",
    "Thumbs.db",
    "__pycache__",
    ".git",
    ".pytest_cache",
    ".ruff_cache",
    "node_modules",
    "data",
    "dist",
    "tests",
}
IGNORED_SUFFIXES = (
    ".db",
    ".db-journal",
    ".db-shm",
    ".db-wal",
    ".sqlite",
    ".sqlite-journal",
    ".sqlite-shm",
    ".sqlite-wal",
    ".sqlite3",
    ".sqlite3-journal",
    ".sqlite3-shm",
    ".sqlite3-wal",
    ".log",
    ".pyc",
    ".pyo",
    ".tmp",
    ".zip",
)
FIXED_ZIP_TIMESTAMP = (1980, 1, 1, 0, 0, 0)
VERSION_RE = re.compile(r"\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?")


def collect_runtime_files(root: Path) -> list[Path]:
    """Collect required runtime modules and WebUI assets, including local edits."""

    selected: set[Path] = set()
    for name in (*RUNTIME_FILES, *OPTIONAL_FILES):
        path = root / name
        if path.is_symlink():
            raise ValueError(f"安装包不允许包含符号链接：{name}")
        if path.is_file():
            selected.add(path)
    for directory_name in RUNTIME_DIRECTORIES:
        directory = root / directory_name
        if directory.is_symlink() or not directory.is_dir():
            raise ValueError(f"运行目录不存在或是符号链接：{directory_name}")
        for path in directory.rglob("*"):
            relative = path.relative_to(root)
            if path.is_symlink():
                raise ValueError(f"安装包不允许包含符号链接：{relative}")
            if (
                path.is_file()
                and not any(part in IGNORED_NAMES for part in relative.parts)
                and not path.name.lower().endswith(IGNORED_SUFFIXES)
            ):
                selected.add(path)
    names = {path.relative_to(root).as_posix() for path in selected}
    missing = sorted(set(REQUIRED_ARCHIVE_FILES) - names)
    if missing:
        raise ValueError(f"缺少运行必需文件：{', '.join(missing)}")
    return sorted(selected, key=lambda path: path.relative_to(root).as_posix())


def package_identity(root: Path) -> tuple[str, str]:
    """Read YAML and Python syntax without importing the plugin or AstrBot."""

    metadata = yaml.safe_load((root / "metadata.yaml").read_text(encoding="utf-8"))
    if not isinstance(metadata, dict):
        raise TypeError("metadata.yaml 必须是映射对象")
    name = str(metadata.get("name") or "")
    version = str(metadata.get("version") or "")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", name):
        raise ValueError("metadata.yaml 中的插件名称不能用作安装包目录名")
    if not VERSION_RE.fullmatch(version):
        raise ValueError(f"插件版本格式无效：{version}")
    tree = ast.parse((root / "main.py").read_text(encoding="utf-8"))
    constants = {
        target.id: node.value.value
        for node in tree.body
        if isinstance(node, ast.Assign)
        for target in node.targets
        if isinstance(target, ast.Name) and isinstance(node.value, ast.Constant)
    }
    registrations = [
        decorator
        for node in tree.body
        if isinstance(node, ast.ClassDef)
        for decorator in node.decorator_list
        if isinstance(decorator, ast.Call)
        and isinstance(decorator.func, ast.Name)
        and decorator.func.id == "register"
    ]
    if len(registrations) != 1 or len(registrations[0].args) < 4:
        raise ValueError("无法确定 main.py 中的 @register 插件名称和版本")
    values = []
    for expression in (registrations[0].args[0], registrations[0].args[3]):
        if isinstance(expression, ast.Constant):
            values.append(expression.value)
        elif isinstance(expression, ast.Name):
            values.append(constants.get(expression.id))
        else:
            values.append(None)
    if values != [name, version]:
        raise ValueError(
            "metadata.yaml 与 main.py 注册信息不一致："
            f"metadata={name}@{version}，register={values[0]}@{values[1]}"
        )
    return name, version


def _archive_info(name: str) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(name, date_time=FIXED_ZIP_TIMESTAMP)
    info.compress_type = zipfile.ZIP_DEFLATED
    info.create_system = 3
    info.external_attr = 0o100644 << 16
    return info


def _validate_archive(path: Path, archive_root: str) -> None:
    with zipfile.ZipFile(path) as archive:
        bad_member = archive.testzip()
        if bad_member:
            raise ValueError(f"安装包 CRC 检查失败：{bad_member}")
        names = archive.namelist()
    missing = {f"{archive_root}/{name}" for name in REQUIRED_ARCHIVE_FILES} - set(names)
    if missing:
        raise ValueError(f"安装包缺少必需文件：{', '.join(sorted(missing))}")
    if len(set(names)) != len(names) or any(
        not name.startswith(f"{archive_root}/")
        or ".." in PurePosixPath(name).parts
        or "\\" in name
        for name in names
    ):
        raise ValueError("安装包包含无效路径、重复文件或多个根目录")


def build_package(root: Path, output: Path | None = None) -> Path:
    """Validate and atomically publish a deterministic ZIP, printing its digest."""

    root = root.resolve()
    files = collect_runtime_files(root)
    plugin_name, version = package_identity(root)
    destination = (
        output.resolve()
        if output is not None
        else root / "dist" / f"{plugin_name}-v{version}.zip"
    )
    if destination.suffix.lower() != ".zip":
        raise ValueError("输出文件必须使用 .zip 扩展名")
    if destination in files or any(
        destination.is_relative_to(root / directory)
        for directory in RUNTIME_DIRECTORIES
    ):
        raise ValueError("不能将安装包写入运行文件目录或覆盖运行文件")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            prefix=f".{destination.stem}-",
            suffix=".tmp",
            dir=destination.parent,
            delete=False,
        ) as temporary:
            temporary_path = Path(temporary.name)
        with zipfile.ZipFile(
            temporary_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9
        ) as archive:
            for source in files:
                relative = source.relative_to(root).as_posix()
                archive.writestr(
                    _archive_info(f"{plugin_name}/{relative}"),
                    source.read_bytes(),
                    compresslevel=9,
                )
        _validate_archive(temporary_path, plugin_name)
        os.replace(temporary_path, destination)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
    digest = hashlib.sha256(destination.read_bytes()).hexdigest()
    print(f"已构建：{destination}")
    print(f"文件数：{len(files)}；大小：{destination.stat().st_size} 字节")
    print(f"SHA256: {digest}")
    return destination


def main() -> int:
    parser = argparse.ArgumentParser(
        description="构建可上传至 AstrBot 的最小插件安装包。"
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
        help="插件工作目录，默认自动定位本仓库",
    )
    parser.add_argument(
        "--output", type=Path, help="输出 ZIP 路径，默认为 dist/<插件名>-v<版本>.zip"
    )
    args = parser.parse_args()
    try:
        build_package(args.root, args.output)
    except (
        OSError,
        ValueError,
        TypeError,
        SyntaxError,
        yaml.YAMLError,
        zipfile.BadZipFile,
    ) as exc:
        parser.exit(1, f"构建失败：{exc}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
