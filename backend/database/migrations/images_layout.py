"""Resumable history/ -> images/ data layout conversion, before gallery startup.

Only owned image paths and published ComfyUI file descriptors are rewritten.
Old files remain until SQLite commits; a durable inventory permits safe retries
after either side of that commit, without trusting a filesystem phase flag.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import stat
import uuid
from contextlib import contextmanager
from pathlib import Path, PurePosixPath

JOURNAL_NAME = ".images-layout-migration.json"
_KINDS = {"assets", "thumbnails"}


@contextmanager
def _migration_lock(root):
    # Keep the lock inode after use so another initializer cannot lock a new
    # inode while an earlier process is still finishing filesystem cleanup.
    path = _safe_path(root, ".images-layout.lock")
    with path.open("a+b") as lock:
        if os.name == "nt":
            import msvcrt

            if path.stat().st_size == 0:
                lock.write(b"\0")
                lock.flush()
            lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl

            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            if os.name == "nt":
                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def _translated(value):
    if not isinstance(value, str):
        return None
    parts = value.replace("\\", "/").split("/")
    if parts[0] != "history":
        return None
    if (
        len(parts) < 3
        or parts[1] not in _KINDS
        or any(part in {"", ".", ".."} or ":" in part for part in parts)
    ):
        raise RuntimeError("旧图片路径不安全，未迁移：" + value)
    return PurePosixPath("images", *parts[1:]).as_posix()


def _safe_path(root, relative):
    """Check every existing ancestor without resolving through a symlink."""
    parts = PurePosixPath(relative).parts
    if not parts or PurePosixPath(relative).is_absolute() or ".." in parts:
        raise RuntimeError("图片迁移路径无效")
    current = root
    for index, part in enumerate(parts):
        current = current / part
        try:
            info = current.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode):
            raise RuntimeError("图片目录迁移不接受符号链接：" + str(current))
        if index < len(parts) - 1 and not stat.S_ISDIR(info.st_mode):
            raise RuntimeError("图片迁移路径的上级不是目录：" + str(current))
        if not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)):
            raise RuntimeError("图片迁移遇到非普通文件：" + str(current))
    return current


def _digest(path):
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def _verify(path, entry):
    if (
        not path.is_file()
        or path.stat().st_size != entry["size"]
        or _digest(path) != entry["sha256"]
    ):
        raise RuntimeError("图片迁移内容冲突或校验失败，保留原文件：" + str(path))


def _sync_directory(path):
    # Windows does not support opening a directory for fsync.
    if os.name == "nt":
        return
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _save_journal(root, journal):
    path = _safe_path(root, JOURNAL_NAME)
    temporary = root / (JOURNAL_NAME + "." + uuid.uuid4().hex + ".tmp")
    try:
        with temporary.open("x", encoding="utf-8") as stream:
            json.dump(journal, stream, ensure_ascii=False, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        _sync_directory(root)
    finally:
        temporary.unlink(missing_ok=True)


def _inventory(root, journal, updates):
    entries = {}
    for entry in journal.get("files", []):
        source = entry["source"]
        if _translated(source) is None or source != source.replace("\\", "/"):
            raise RuntimeError("图片迁移记录中的路径无效")
        if (
            not isinstance(entry.get("size"), int)
            or entry["size"] < 0
            or len(entry.get("sha256", "")) != 64
        ):
            raise RuntimeError("图片迁移记录中的校验值无效")
        if source in entries:
            raise RuntimeError("图片迁移记录存在重复路径")
        entries[source] = entry
    # Only registered files belong to this conversion. Moving unknown files
    # into images/ would expose them to the ordinary orphan sweep on startup.
    # A user or an interrupted upgrade may already have moved known files.
    for table, _key, identity, old_path, new_path in updates:
        normalized_old = old_path.replace("\\", "/")
        old = _safe_path(root, normalized_old)
        new = _safe_path(root, new_path)
        if old.exists() and not old.is_file():
            raise RuntimeError("旧图片路径不是普通文件：" + old_path)
        if new.exists() and not new.is_file():
            raise RuntimeError("新图片路径不是普通文件：" + new_path)
        existing = old if old.is_file() else new
        if normalized_old not in entries and existing.is_file():
            digest = _digest(existing)
            if (
                table == "image_assets"
                and len(identity) == 64
                and all(c in "0123456789abcdef" for c in identity)
                and identity != digest
            ):
                raise RuntimeError("原图内容与资产哈希不符：" + old_path)
            entries[normalized_old] = {
                "source": normalized_old,
                "size": existing.stat().st_size,
                "sha256": digest,
            }
    # Preflight every destination before copying or rewriting any database row.
    directories = set()
    for entry in entries.values():
        old = _safe_path(root, entry["source"])
        new = _safe_path(root, _translated(entry["source"]))
        if old.exists():
            _verify(old, entry)
        if new.exists():
            _verify(new, entry)
        elif not old.exists():
            raise RuntimeError("图片迁移记录的源文件和目标文件均已丢失")
        directory = old.parent
        while directory != root / "history":
            if directory.exists():
                directories.add(directory)
            directory = directory.parent
    # Empty known roots may be left by an interrupted cleanup or an empty
    # installation; rmdir will keep any unregistered files under these roots.
    for kind in _KINDS:
        directory = _safe_path(root, "history/" + kind)
        if directory.is_dir():
            directories.add(directory)
    return list(entries.values()), directories


def _stage_file(root, entry, migration_id):
    old = _safe_path(root, entry["source"])
    new = _safe_path(root, _translated(entry["source"]))
    if new.exists():
        _verify(new, entry)
        return
    new.parent.mkdir(parents=True, exist_ok=True)
    _verify(old, entry)
    try:
        os.link(old, new)
    except FileExistsError:
        _verify(new, entry)
    except OSError:
        # The journal owns this unique temporary name; a interrupted copy can be
        # replaced without ever touching an unrelated destination file.
        temporary = _safe_path(
            root, new.relative_to(root).as_posix() + "." + migration_id + ".tmp"
        )
        temporary.unlink(missing_ok=True)
        try:
            with old.open("rb") as source, temporary.open("xb") as target:
                shutil.copyfileobj(source, target)
                target.flush()
                shutil.copystat(old, temporary, follow_symlinks=False)
                os.fsync(target.fileno())
            _verify(temporary, entry)
            if new.exists():
                _verify(new, entry)
            else:
                temporary.rename(new)
        finally:
            temporary.unlink(missing_ok=True)
    _verify(new, entry)


def _database_updates(conn):
    updates = []
    for table, key in (("image_assets", "id"), ("image_thumbnails", "asset_id")):
        paths = set()
        for identity, path in conn.execute(f"SELECT {key},path FROM {table}"):
            replacement = _translated(path)
            candidate = replacement or path
            if candidate in paths:
                raise RuntimeError("新旧图片数据库路径冲突，未迁移")
            paths.add(candidate)
            if replacement is not None:
                updates.append((table, key, identity, path, replacement))
    jobs = []
    for identity, source in conn.execute(
        "SELECT id,output_refs_json FROM comfy_jobs WHERE output_refs_json!='[]'"
    ):
        outputs = json.loads(source)
        changed = False
        for item in outputs:
            if item.get("storage") == "gallery" and (
                replacement := _translated(item.get("path"))
            ):
                item["path"] = replacement
                changed = True
        if changed:
            jobs.append(
                (
                    identity,
                    source,
                    json.dumps(outputs, ensure_ascii=False, separators=(",", ":")),
                )
            )
    return updates, jobs


def _commit_paths(conn, updates, jobs):
    for table, key, identity, old, new in updates:
        if (
            conn.execute(
                f"UPDATE {table} SET path=? WHERE {key}=? AND path=?",
                (new, identity, old),
            ).rowcount
            != 1
        ):
            raise RuntimeError("迁移期间图片记录发生变化，未提交")
    for identity, old, new in jobs:
        if (
            conn.execute(
                "UPDATE comfy_jobs SET output_refs_json=? WHERE id=? AND output_refs_json=?",
                (new, identity, old),
            ).rowcount
            != 1
        ):
            raise RuntimeError("迁移期间任务输出记录发生变化，未提交")
    conn.commit()


def migrate_images_layout(conn: sqlite3.Connection, data_dir: Path) -> dict[str, int]:
    """Run before background jobs, scanning or any gallery repair/cleanup.

    No schema version changes: path values move in one SQLite transaction. The
    journal is deliberately retained on failure; the next startup validates and
    completes that inventory before any orphan cleanup is allowed to run.
    """
    root = data_dir.resolve()
    if conn.in_transaction:
        raise RuntimeError("图片目录迁移需要独立事务")
    with _migration_lock(root):
        return _migrate_locked(conn, root)


def _migrate_locked(conn, root):
    for name in ("history", "images", "images/assets", "images/thumbnails"):
        path = _safe_path(root, name)
        if path.exists() and not path.is_dir():
            raise RuntimeError("图片存储路径不是目录：" + str(path))
    journal_path = _safe_path(root, JOURNAL_NAME)
    journal = {"version": 1, "id": uuid.uuid4().hex, "files": []}
    if journal_path.exists():
        journal = json.loads(journal_path.read_text(encoding="utf-8"))
        if (
            journal.get("version") != 1
            or not isinstance(journal.get("id"), str)
            or len(journal["id"]) != 32
            or any(c not in "0123456789abcdef" for c in journal["id"])
        ):
            raise RuntimeError("图片迁移记录无效，未继续迁移")
    result = {"files": 0, "asset_paths": 0, "job_manifests": 0}
    conn.execute("BEGIN IMMEDIATE")
    try:
        updates, jobs = _database_updates(conn)
        entries, directories = _inventory(root, journal, updates)
        if (
            not entries
            and not updates
            and not jobs
            and not journal_path.exists()
            and not directories
        ):
            conn.rollback()
            return result
        journal["files"] = entries
        _save_journal(root, journal)
        for entry in entries:
            _stage_file(root, entry, journal["id"])
        target_directories = {root}
        for entry in entries:
            directory = (root / _translated(entry["source"])).parent
            while directory != root:
                target_directories.add(directory)
                directory = directory.parent
        for directory in sorted(
            target_directories, key=lambda p: len(p.parts), reverse=True
        ):
            _sync_directory(directory)
        _commit_paths(conn, updates, jobs)
        result.update(
            files=len(entries), asset_paths=len(updates), job_manifests=len(jobs)
        )
    except BaseException:
        conn.rollback()
        raise
    # Database commit is the point of no return for the old paths. Keep both
    # copies on any verification failure, and let the next startup resume.
    for entry in entries:
        old = _safe_path(root, entry["source"])
        if old.exists():
            _verify(old, entry)
            _verify(_safe_path(root, _translated(entry["source"])), entry)
            old.unlink()
    for directory in sorted(
        directories, key=lambda path: len(path.parts), reverse=True
    ):
        try:
            directory.rmdir()
        except OSError:
            pass
    try:
        (root / "history").rmdir()
    except OSError:
        pass
    journal_path.unlink(missing_ok=True)
    _sync_directory(root)
    return result
