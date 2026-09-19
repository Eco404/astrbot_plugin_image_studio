"""Atomic writes and deletion constrained to plugin-owned directories."""

from __future__ import annotations

import os
import uuid
from pathlib import Path


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_bytes(data)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except (OSError, ValueError):
        return False


def _unlink_if_owned(path: Path, root: Path) -> None:
    try:
        if not root.is_symlink() and not path.is_symlink() and _is_within(path, root):
            path.unlink(missing_ok=True)
    except OSError:
        pass


def _delete_unreferenced_files(
    root: Path,
    referenced: set[Path],
    *,
    older_than: float | None = None,
) -> int:
    if root.is_symlink():
        return 0
    removed = 0
    for path in root.rglob("*"):
        try:
            if (
                path.is_file()
                and not path.is_symlink()
                and _is_within(path, root)
                and path.resolve() not in referenced
                and (older_than is None or path.stat().st_mtime < older_than)
            ):
                path.unlink()
                removed += 1
        except OSError:
            continue
    return removed


def _remove_empty_directories(root: Path) -> None:
    if root.is_symlink():
        return
    directories = sorted(
        (
            path
            for path in root.rglob("*")
            if path.is_dir() and not path.is_symlink() and _is_within(path, root)
        ),
        key=lambda path: len(path.parts),
        reverse=True,
    )
    for path in directories:
        try:
            path.rmdir()
        except OSError:
            continue
