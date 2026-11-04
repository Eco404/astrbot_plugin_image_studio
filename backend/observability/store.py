"""Daily JSON-lines logs, with bounded payloads and streaming queries."""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import io
import json
import math
import os
import re
import stat
import threading
import uuid
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from astrbot.api import logger

LEVELS = {"DEBUG": 10, "INFO": 20, "WARNING": 30, "ERROR": 40}
CATEGORIES = ("request", "generation", "operation", "system")
MAX_ENTRY_BYTES = 192 * 1024
_FILE_NAME = re.compile(r"\d{4}-\d{2}-\d{2}\.log\Z")
_SECRET = re.compile(
    r"(?:authorization|cookie|password|passwd|secret|credential|api[-_]?key|"
    r"access[-_]?key|private[-_]?key|token|signature|^key$|^auth$)",
    re.I,
)
_DATA_URI = re.compile(r"data:([^;,\s]+)?;base64,([A-Za-z0-9+/=_\-\r\n]+)", re.I)
_URL = re.compile(r"(?:https?|wss?)://[^\s<>\"']+", re.I)
_INLINE_SECRET = re.compile(
    r"((?:authorization|api[-_ ]?key|password|access[-_]?token|secret)[\"']?\s*[:=]\s*[\"']?)"
    r"(?:Bearer\s+)?[^\s,;\"'}]+",
    re.I,
)
_CONTEXT = contextvars.ContextVar("image_studio_log_context", default={})
_SAFE_HEADERS = {
    "accept",
    "content-type",
    "content-length",
    "user-agent",
    "accept-encoding",
    "content-encoding",
    "content-disposition",
    "connection",
    "cache-control",
}


@contextlib.contextmanager
def log_context(**values: Any) -> Iterator[None]:
    """Correlate child tasks/HTTP requests with their operation or generation."""
    current = _CONTEXT.get()
    if current.get("operation_id"):
        values["operation_id"] = current["operation_id"]
    token = _CONTEXT.set({**current, **values})
    try:
        yield
    finally:
        _CONTEXT.reset(token)


def _safe_url(value: str) -> str:
    try:
        parsed = urlsplit(value)
        netloc = parsed.netloc.rsplit("@", 1)[-1]
        if "@" in parsed.netloc:
            netloc = "[redacted]@" + netloc
        query = urlencode(
            [
                (key, "[redacted]" if _SECRET.search(key) else val)
                for key, val in parse_qsl(parsed.query, keep_blank_values=True)
            ]
        )
        # URL fragments can contain access credentials and are never sent to HTTP.
        return urlunsplit((parsed.scheme, netloc, parsed.path, query, ""))
    except ValueError:
        return "[invalid URL omitted]"


def sanitize(value: Any, *, max_chars: int = 128 * 1024) -> Any:
    """Never serialize credentials, binary images, streams, or unbounded objects.

    Limits apply to the complete tree, including nested dictionaries. Unknown
    objects are described by class name rather than repr(), which may hold keys.
    """
    remaining = max_chars
    nodes = 0

    def visit(item: Any, depth: int = 0, field: str = "") -> Any:
        nonlocal remaining, nodes
        nodes += 1
        if remaining <= 0 or nodes > 10000:
            return "[size limit]"
        if _SECRET.search(field):
            return "[redacted]"
        if item is None or isinstance(item, (bool, int)):
            remaining -= 24
            return item
        if isinstance(item, float):
            remaining -= 24
            return item if math.isfinite(item) else str(item)
        if isinstance(item, (bytes, bytearray, memoryview)):
            remaining -= 64
            return {"kind": "binary", "bytes": len(item)}
        if isinstance(item, io.IOBase) or callable(getattr(item, "read", None)):
            remaining -= 64
            return {"kind": "stream", "type": type(item).__name__}
        if depth >= 12:
            return "[depth limit]"
        if isinstance(item, str):
            text = _DATA_URI.sub(
                lambda match: (
                    f"[data URI: {match[1] or 'application/octet-stream'}, {len(match[2])} base64 characters]"
                ),
                item,
            )
            if len(text) > 128 and re.fullmatch(r"[A-Za-z0-9+/=_\-\r\n]+", text):
                text = f"[base64/binary text: {len(text)} characters]"
            text = _URL.sub(lambda match: _safe_url(match[0]), text)
            text = _INLINE_SECRET.sub(lambda match: match[1] + "[redacted]", text)
            text = re.sub(
                r"\bBearer\s+[A-Za-z0-9._~+/=-]+", "Bearer [redacted]", text, flags=re.I
            )
            limit = max(0, min(16384, remaining))
            if len(text) > limit:
                text = text[:limit] + "… [truncated]"
            remaining -= len(text) + 8
            return text
        if isinstance(item, dict) or hasattr(item, "items"):
            result = {}
            for index, (key, val) in enumerate(item.items()):
                if index >= 300 or remaining <= 0 or nodes > 10000:
                    result["_truncated"] = True
                    break
                key = str(key)[:256]
                remaining -= len(key) + 8
                if field.lower() == "headers" and key.lower() not in _SAFE_HEADERS:
                    result[key] = "[redacted]"
                else:
                    result[key] = visit(val, depth + 1, key)
            return result
        if isinstance(item, (list, tuple, set, frozenset)):
            result = []
            for index, val in enumerate(item):
                if index >= 300 or remaining <= 0 or nodes > 10000:
                    result.append("[truncated]")
                    break
                result.append(visit(val, depth + 1))
            return result
        return {"kind": type(item).__name__}

    return visit(value)


def _reverse_lines(path: Path, snapshot: os.stat_result) -> Iterator[bytes]:
    """Read bounded chunks; corrupt oversized lines cannot exhaust memory."""
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    with os.fdopen(os.open(path, flags), "rb") as stream:
        info = os.fstat(stream.fileno())
        if (info.st_dev, info.st_ino) != (snapshot.st_dev, snapshot.st_ino):
            return
        position, remainder, discard = min(info.st_size, snapshot.st_size), b"", False
        while position:
            size = min(position, 65536)
            position -= size
            stream.seek(position)
            parts = (stream.read(size) + remainder).split(b"\n")
            remainder = parts.pop(0)
            for line in reversed(parts):
                if discard:
                    discard = False
                elif 0 < len(line) <= MAX_ENTRY_BYTES:
                    yield line
            if len(remainder) > MAX_ENTRY_BYTES:
                remainder = b""
                discard = True
        if remainder and not discard and len(remainder) <= MAX_ENTRY_BYTES:
            yield remainder


class StructuredLogStore:
    def __init__(
        self,
        data_dir: Path,
        *,
        enabled: bool = True,
        level: str = "INFO",
        retention_days: int = 7,
    ) -> None:
        self.directory = Path(data_dir) / "logs"
        self._lock = threading.RLock()
        self._cleanup_day: date | None = None
        self.configure(enabled=enabled, level=level, retention_days=retention_days)

    def configure(self, *, enabled: bool, level: str, retention_days: int) -> None:
        level = str(level).upper()
        if level not in LEVELS:
            raise ValueError("日志等级必须为 DEBUG、INFO、WARNING 或 ERROR")
        if isinstance(retention_days, bool) or int(retention_days) < 0:
            raise ValueError("日志保留天数不能为负数")
        with self._lock:
            self.enabled, self.level = bool(enabled), level
            self.retention_days = int(retention_days)
            self._cleanup_day = None

    def accepts(self, level: str) -> bool:
        return self.enabled and LEVELS.get(level.upper(), 0) >= LEVELS[self.level]

    def status(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "level": self.level,
            "retention_days": self.retention_days,
        }

    async def record(
        self,
        level: str,
        category: str,
        message: str,
        data: Any = None,
        *,
        request_id: str | None = None,
        operation_id: str | None = None,
    ) -> None:
        """Logging failure must never fail an image request or user operation."""
        level = level.upper()
        if not self.accepts(level):
            return
        try:
            context = dict(_CONTEXT.get())
            if request_id is not None:
                context["request_id"] = request_id
            if operation_id is not None:
                context["operation_id"] = operation_id
            entry = {
                "id": uuid.uuid4().hex,
                "timestamp": datetime.now(timezone.utc).isoformat(
                    timespec="milliseconds"
                ),
                "level": level,
                "category": category if category in CATEGORIES else "system",
                "message": sanitize(str(message), max_chars=4096),
                "context": sanitize(context, max_chars=8192),
                "data": sanitize(data),
            }
            for key in ("request_id", "operation_id"):
                if key in entry["context"]:
                    entry[key] = entry["context"][key]
            await asyncio.to_thread(self._append, entry)
        except Exception as exc:
            logger.warning("[ImageStudio] 日志写入失败 (%s)", type(exc).__name__)

    def _files(self) -> list[Path]:
        if self.directory.is_symlink() or not self.directory.is_dir():
            return []
        return sorted(
            (
                path
                for path in self.directory.iterdir()
                if _FILE_NAME.fullmatch(path.name)
                and stat.S_ISREG(path.lstat().st_mode)
            ),
            reverse=True,
        )

    def _append(self, entry: dict[str, Any]) -> None:
        with self._lock:
            if not self.accepts(entry["level"]):
                return
            if self.directory.is_symlink():
                raise OSError("Log directory is a symlink")
            self.directory.mkdir(parents=True, exist_ok=True)
            now = datetime.now(timezone.utc)
            if self._cleanup_day != now.date():
                self._cleanup(now)
            encoded = json.dumps(entry, ensure_ascii=False, separators=(",", ":"))
            if len(encoded.encode("utf-8")) > MAX_ENTRY_BYTES:
                entry["data"] = {"_truncated": True, "reason": "entry size limit"}
                encoded = json.dumps(entry, ensure_ascii=False, separators=(",", ":"))
            path = self.directory / (now.date().isoformat() + ".log")
            # No persistent handle: rotation and clear are atomic with append.
            flags = (
                os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0)
            )
            with os.fdopen(
                os.open(path, flags, 0o600), "a", encoding="utf-8"
            ) as stream:
                stream.write(encoded + "\n")

    async def query(
        self,
        *,
        category: str = "",
        level: str = "",
        date_from: str = "",
        date_to: str = "",
        page: int = 1,
        page_size: int = 50,
    ) -> dict[str, Any]:
        level = level.upper()
        if category and category not in CATEGORIES:
            raise ValueError("日志类型无效")
        if level and level not in LEVELS:
            raise ValueError("日志等级无效")
        start = date.fromisoformat(date_from) if date_from else None
        end = date.fromisoformat(date_to) if date_to else None
        if start and end and start > end:
            raise ValueError("开始日期不能晚于结束日期")
        page, page_size = max(1, int(page)), min(200, max(1, int(page_size)))
        return await asyncio.to_thread(
            self._query, category, level, start, end, page, page_size
        )

    def _query(self, category, level, start, end, page, page_size) -> dict[str, Any]:
        with self._lock:
            snapshots = [(path, path.stat()) for path in self._files()]
        # A large historical query must not make generation wait for log writes.
        # Snapshot lengths exclude partial/new writes. An unlinked file remains
        # readable through its open descriptor; a replaced inode is skipped.
        items, total, offset = [], 0, (page - 1) * page_size
        for path, snapshot in snapshots:
            try:
                day = date.fromisoformat(path.stem)
            except ValueError:
                continue
            if (start and day < start) or (end and day > end):
                continue
            try:
                for line in _reverse_lines(path, snapshot):
                    try:
                        entry = json.loads(line)
                    except (ValueError, UnicodeError, RecursionError):
                        continue
                    if not isinstance(entry, dict):
                        continue
                    if category and entry.get("category") != category:
                        continue
                    if level and entry.get("level") != level:
                        continue
                    if offset <= total < offset + page_size:
                        items.append(entry)
                    total += 1
            except FileNotFoundError:
                # Clear/retention may remove a file after the short snapshot lock.
                continue
        return {
            **self.status(),
            "items": items,
            "total": total,
            "page": page,
            "page_size": page_size,
            "has_more": offset + len(items) < total,
            "categories": list(CATEGORIES),
            "levels": list(LEVELS),
            "timezone": "UTC",
        }

    async def clear(self) -> dict[str, int]:
        return await asyncio.to_thread(self._clear)

    def _clear(self) -> dict[str, int]:
        with self._lock:
            removed, size = 0, 0
            for path in self._files():
                size += path.stat().st_size
                path.unlink()
                removed += 1
            return {"files_removed": removed, "bytes_reclaimed": size}

    async def cleanup(self) -> dict[str, int]:
        # Also called by plugin maintenance when no new log entries arrive.
        try:
            return await asyncio.to_thread(self._cleanup, datetime.now(timezone.utc))
        except OSError as exc:
            logger.warning("[ImageStudio] 日志清理失败 (%s)", type(exc).__name__)
            return {"files_removed": 0, "bytes_reclaimed": 0}

    def _cleanup(self, now: datetime) -> dict[str, int]:
        with self._lock:
            removed, size = 0, 0
            if self.retention_days:
                cutoff = date.fromordinal(
                    max(1, now.date().toordinal() - self.retention_days + 1)
                )
                for path in self._files():
                    try:
                        day = date.fromisoformat(path.stem)
                    except ValueError:
                        continue
                    if day < cutoff:
                        size += path.stat().st_size
                        path.unlink()
                        removed += 1
            self._cleanup_day = now.date()
            return {"files_removed": removed, "bytes_reclaimed": size}

    async def usage(self) -> dict[str, int]:
        def read():
            with self._lock:
                files = self._files()
                return {
                    "file_count": len(files),
                    "file_bytes": sum(path.stat().st_size for path in files),
                }

        return await asyncio.to_thread(read)
