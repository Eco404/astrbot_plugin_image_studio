from __future__ import annotations

import importlib.util
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "build_plugin_package.py"
SPEC = importlib.util.spec_from_file_location("image_studio_package_builder", SCRIPT)
builder = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(builder)


@pytest.fixture
def source_tree(tmp_path):
    root = tmp_path / "plugin"
    for name in builder.REQUIRED_ARCHIVE_FILES:
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("runtime", encoding="utf-8")
    (root / "metadata.yaml").write_text(
        'name: "astrbot_plugin_image_studio"\nversion: "1.0.0" # release\n',
        encoding="utf-8",
    )
    (root / "main.py").write_text(
        'PLUGIN_NAME = "astrbot_plugin_image_studio"\n'
        '@register(PLUGIN_NAME, "econeco", "Description", "1.0.0")\n'
        "class ImageStudioPlugin: pass\n",
        encoding="utf-8",
    )
    return root


def test_real_package_contains_all_runtime_assets_and_is_reproducible(tmp_path):
    outputs = [tmp_path / "first.zip", tmp_path / "second.zip"]
    for output in outputs:
        result = subprocess.run(
            [sys.executable, str(SCRIPT), "--output", str(output)],
            cwd=tmp_path,
            check=True,
            capture_output=True,
            text=True,
        )
        assert "SHA256:" in result.stdout
    assert outputs[0].read_bytes() == outputs[1].read_bytes()
    with zipfile.ZipFile(outputs[0]) as archive:
        assert archive.testzip() is None
        name, version = builder.package_identity(ROOT)
        expected = {f"{name}/{item}" for item in builder.REQUIRED_ARCHIVE_FILES}
        assert expected.issubset(archive.namelist())
        assert archive.read(f"{name}/main.py") == (ROOT / "main.py").read_bytes()
        metadata = yaml.safe_load(archive.read(f"{name}/metadata.yaml"))
        assert metadata["author"] == "econeco"
        assert (
            metadata["repo"] == "https://github.com/Eco404/astrbot_plugin_image_studio"
        )
        changelog = archive.read(f"{name}/CHANGELOG.md").decode("utf-8")
        assert f"## {version} - " in changelog
        readme = archive.read(f"{name}/README.md").decode("utf-8")
        assert "](docs/images/generate.png)" in readme
        for relative in ("docs/images/generate.png", "logo.png"):
            image = archive.read(f"{name}/{relative}")
            assert image == (ROOT / relative).read_bytes()
            assert image.startswith(b"\x89PNG\r\n\x1a\n")
        for member in archive.infolist():
            assert not {
                "data",
                "tests",
                "scripts",
                ".git",
                "__pycache__",
                "node_modules",
            }.intersection(Path(member.filename).parts)
            if "docs" in Path(member.filename).parts:
                assert member.filename == f"{name}/docs/images/generate.png"
            assert member.date_time == builder.FIXED_ZIP_TIMESTAMP
            assert member.external_attr >> 16 == 0o100644


def test_allowlist_includes_worktree_changes_but_not_runtime_residue(source_tree):
    for name in (
        "data/private.png",
        "pages/debug.sqlite3",
        "pages/debug.sqlite3-wal",
        "pages/debug.log",
        "pages/__pycache__/module.pyc",
        "pages/node_modules/module.js",
        "docs/internal.md",
        "docs/images/private.sqlite3",
        "docs/images/generate.webp",
        "docs/images/gallery.webp",
    ):
        target = source_tree / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("private", encoding="utf-8")
    (source_tree / "providers.py").write_text("uncommitted content", encoding="utf-8")
    output = builder.build_package(source_tree)
    with zipfile.ZipFile(output) as archive:
        assert len(archive.namelist()) == len(builder.REQUIRED_ARCHIVE_FILES)
        assert (
            archive.read("astrbot_plugin_image_studio/providers.py")
            == b"uncommitted content"
        )


def test_mismatched_version_missing_module_and_symlink_fail(source_tree, tmp_path):
    metadata = source_tree / "metadata.yaml"
    original = metadata.read_text(encoding="utf-8")
    metadata.write_text(original.replace("1.0.0", "1.0.1"), encoding="utf-8")
    with pytest.raises(ValueError, match="不一致"):
        builder.build_package(source_tree)
    metadata.write_text(original, encoding="utf-8")
    module = source_tree / "parameter_exchange.py"
    module.unlink()
    with pytest.raises(ValueError, match="parameter_exchange.py"):
        builder.build_package(source_tree)
    module.symlink_to(tmp_path / "external.py")
    with pytest.raises(ValueError, match="符号链接"):
        builder.build_package(source_tree)


def test_invalid_archive_does_not_replace_previous_output(
    source_tree, tmp_path, monkeypatch
):
    output = tmp_path / "previous.zip"
    output.write_bytes(b"previous build")

    def invalid(*_args):
        raise ValueError("CRC failure")

    monkeypatch.setattr(builder, "_validate_archive", invalid)
    with pytest.raises(ValueError, match="CRC"):
        builder.build_package(source_tree, output)
    assert output.read_bytes() == b"previous build"
    assert not list(tmp_path.glob(".previous-*.tmp"))


def test_output_cannot_be_written_into_runtime_directory(source_tree):
    with pytest.raises(ValueError, match="运行文件目录"):
        builder.build_package(source_tree, source_tree / "pages" / "package.zip")
