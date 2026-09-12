"""Background indexing of other plugins' galleries without owning their originals."""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import re
import stat
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from .image_metadata import MAX_IMAGE_BYTES

MAX_SIDECAR_BYTES = 1024 * 1024
MAX_SIDECAR_NODES = 4096
MAX_SIDECAR_DEPTH = 20
_MAX_REPORTED_ERRORS = 20
_LOGGER = logging.getLogger(__name__)


def file_fingerprint(value: os.stat_result) -> dict[str, int]:
    """Stat identity used for incremental reads and guarded deletion."""
    return {
        "size_bytes": value.st_size,
        "mtime_ns": value.st_mtime_ns,
        "ctime_ns": value.st_ctime_ns,
        "device": value.st_dev,
        "inode": value.st_ino,
    }


def _fingerprint_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


class _BoundedSafeLoader(yaml.SafeLoader):
    """Sidecars contain flat request fields; aliases and huge graphs are unnecessary."""

    def __init__(self, stream: str) -> None:
        super().__init__(stream)
        self._node_count = 0
        self._node_depth = 0

    def compose_node(self, parent: Any, index: Any) -> Any:
        if self.check_event(yaml.AliasEvent):
            raise ValueError("参数文件不支持 YAML 引用")
        self._node_count += 1
        self._node_depth += 1
        try:
            if self._node_count > MAX_SIDECAR_NODES:
                raise ValueError("参数文件包含过多字段")
            if self._node_depth > MAX_SIDECAR_DEPTH:
                raise ValueError("参数文件嵌套过深")
            return super().compose_node(parent, index)
        finally:
            self._node_depth -= 1


class _FileChangedError(ValueError):
    pass


@dataclass(frozen=True)
class ExternalGalleryAdapter:
    """A source only describes paths and request fields; storage owns indexing."""

    id: str
    name: str
    plugin_directory: str
    history_directory: str
    generation_engine: str = "unknown"

    def resolve_root(self, data_dir: Path) -> Path:
        return data_dir.parent / self.plugin_directory / self.history_directory

    def accepts(self, filename: str) -> bool:
        raise NotImplementedError

    def sidecar_paths(self, image_path: Path) -> tuple[Path, ...]:
        return ()

    def request_parameters(self, data: bytes, filename: str) -> dict[str, Any]:
        return {}

    def created_at(self, filename: str, mtime_ns: int) -> float:
        return mtime_ns / 1_000_000_000


class NAIGalleryAdapter(ExternalGalleryAdapter):
    _fields = frozenset(
        {
            "tag",
            "model",
            "artist",
            "size",
            "steps",
            "scale",
            "cfg",
            "sampler",
            "negative",
            "nocache",
            "noise_schedule",
            "seed",
        }
    )

    def __init__(self) -> None:
        super().__init__(
            "nai",
            "NAI 插件图库",
            "astrbot_plugin_nai_image",
            "image_history",
            "novelai",
        )

    def accepts(self, filename: str) -> bool:
        return filename.startswith("nai_") and Path(filename).suffix.lower() in {
            ".png",
            ".jpg",
            ".webp",
            ".img",
        }

    def sidecar_paths(self, image_path: Path) -> tuple[Path, ...]:
        # The current plugin writes YAML; JSON is a legacy fallback only.
        return image_path.with_suffix(".yaml"), image_path.with_suffix(".json")

    def request_parameters(self, data: bytes, filename: str) -> dict[str, Any]:
        text = data.decode("utf-8-sig")
        value = (
            json.loads(text)
            if Path(filename).suffix.lower() == ".json"
            else yaml.load(text, Loader=_BoundedSafeLoader)
        )
        if not isinstance(value, dict):
            raise ValueError("参数文件顶层必须是对象")  # noqa: TRY004 - malformed serialized input
        result: dict[str, Any] = {}
        for key in self._fields:
            item = value.get(key)
            if isinstance(item, (str, bool, int)) or (
                isinstance(item, float) and math.isfinite(item)
            ):
                result[key] = item
        return result

    def created_at(self, filename: str, mtime_ns: int) -> float:
        match = re.fullmatch(r"nai_(\d{16,20})\.[^.]+", filename)
        if match:
            timestamp = int(match[1]) / 1_000_000_000
            if 0 < timestamp < 253402300800:
                return timestamp
        return super().created_at(filename, mtime_ns)


def _sidecar_stats(adapter: ExternalGalleryAdapter, image_path: Path) -> dict:
    result = {}
    for path in adapter.sidecar_paths(image_path):
        try:
            value = path.lstat()
        except FileNotFoundError:
            continue
        if not stat.S_ISREG(value.st_mode):
            raise ValueError(f"{path.name} 不是普通参数文件")
        result[path.name] = file_fingerprint(value)
    return result


def _read_file(path: Path, expected: dict, limit: int) -> bytes:
    if expected["size_bytes"] > limit:
        raise ValueError(f"{path.name} 文件过大")
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(descriptor, "rb") as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode) or file_fingerprint(before) != expected:
            raise _FileChangedError(f"{path.name} 扫描期间发生变化，将在下次扫描重试")
        data = stream.read(limit + 1)
        if len(data) > limit:
            raise ValueError(f"{path.name} 文件过大")
        if file_fingerprint(os.fstat(stream.fileno())) != expected:
            raise _FileChangedError(f"{path.name} 扫描期间发生变化，将在下次扫描重试")
    if file_fingerprint(path.lstat()) != expected:
        raise _FileChangedError(f"{path.name} 扫描期间发生变化，将在下次扫描重试")
    return data


def _enumerate_source(
    adapter: ExternalGalleryAdapter, root: Path
) -> tuple[list, set, dict, list]:
    root_stat = root.lstat()
    if not stat.S_ISDIR(root_stat.st_mode):
        raise NotADirectoryError("图库路径不是文件夹")
    entries, seen, errors = [], set(), []
    with os.scandir(root) as directory:
        for entry in directory:
            if not adapter.accepts(entry.name):
                continue
            # Retain an existing index on unreadable/replaced files. Only a full,
            # successful scan can decide that an original has disappeared.
            seen.add(entry.name)
            try:
                value = entry.stat(follow_symlinks=False)
                if not stat.S_ISREG(value.st_mode):
                    raise ValueError("不是普通图片文件")
                sidecars = _sidecar_stats(adapter, root / entry.name)
                entries.append((entry.name, file_fingerprint(value), sidecars))
            except (OSError, ValueError) as exc:
                errors.append(f"{entry.name}：{exc}")
    entries.sort(key=lambda item: (item[1]["mtime_ns"], item[0]))
    return entries, seen, file_fingerprint(root_stat), errors


def _load_entry(adapter: ExternalGalleryAdapter, root: Path, entry: tuple) -> tuple:
    filename, fingerprint, sidecars = entry
    path = root / filename
    data = _read_file(path, fingerprint, MAX_IMAGE_BYTES)
    parameters, errors = {}, []
    for sidecar in adapter.sidecar_paths(path):
        expected = sidecars.get(sidecar.name)
        if expected is None:
            continue
        try:
            raw = _read_file(sidecar, expected, MAX_SIDECAR_BYTES)
            parameters = adapter.request_parameters(raw, sidecar.name)
            break
        except (UnicodeError, ValueError, yaml.YAMLError, RecursionError) as exc:
            if isinstance(exc, _FileChangedError):
                raise
            if isinstance(exc, yaml.YAMLError):
                mark = getattr(exc, "problem_mark", None)
                detail = f"第 {mark.line + 1} 行格式错误" if mark else "YAML 格式错误"
            elif isinstance(exc, UnicodeError):
                detail = "参数文件不是有效的 UTF-8 文本"
            elif isinstance(exc, RecursionError):
                detail = "参数文件嵌套过深"
            else:
                detail = str(exc)
            errors.append(f"{sidecar.name}：{detail}")
    if _sidecar_stats(adapter, path) != sidecars:
        raise _FileChangedError(f"{filename} 的参数文件发生变化，将在下次扫描重试")
    return data, parameters, errors


async def _settle_mutation(awaitable: Any) -> Any:
    """Cancellation must not abandon a storage worker that could commit later."""
    task = asyncio.ensure_future(awaitable)
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        try:
            await asyncio.shield(task)
        except Exception:
            _LOGGER.exception("External gallery mutation failed during cancellation")
        raise


class ExternalGalleryManager:
    def __init__(
        self,
        store: Any,
        data_dir: Path,
        *,
        preview_max_edge: int = 768,
        preview_quality: int = 80,
        scan_interval: float = 300,
        adapters: list[ExternalGalleryAdapter] | None = None,
    ) -> None:
        self.store = store
        self.data_dir = Path(data_dir).resolve()
        source_adapters = adapters if adapters is not None else [NAIGalleryAdapter()]
        self.adapters = {item.id: item for item in source_adapters}
        if len(self.adapters) != len(source_adapters):
            raise ValueError("外部图库来源 ID 重复")
        self.preview_max_edge = preview_max_edge
        self.preview_quality = preview_quality
        self.scan_interval = max(0.01, float(scan_interval))
        self._enabled = {source_id: False for source_id in self.adapters}
        self._epochs = {source_id: 0 for source_id in self.adapters}
        self._live: dict[str, dict[str, Any]] = {}
        self._tasks: dict[str, asyncio.Task] = {}
        self._periodic_task: asyncio.Task | None = None
        self._started = False
        self._configuration_lock = asyncio.Lock()

    async def configure(
        self,
        external_sources: dict | None,
        *,
        preview_max_edge: int | None = None,
        preview_quality: int | None = None,
    ) -> None:
        async with self._configuration_lock:
            if preview_max_edge is not None:
                self.preview_max_edge = preview_max_edge
            if preview_quality is not None:
                self.preview_quality = preview_quality
            settings = external_sources if isinstance(external_sources, dict) else {}
            for source_id, adapter in self.adapters.items():
                setting = settings.get(source_id, False)
                enabled = (
                    bool(setting.get("enabled", False))
                    if isinstance(setting, dict)
                    else bool(setting)
                )
                changed = enabled != self._enabled[source_id]
                if changed:
                    self._epochs[source_id] += 1
                self._enabled[source_id] = enabled
                if not enabled:
                    await self._stop_source(source_id)
                    self._live[source_id] = {
                        "status": "disabled",
                        "processed": 0,
                        "total": 0,
                    }
                await _settle_mutation(
                    self.store.configure_external_source(
                        source_id,
                        adapter.name,
                        str(adapter.resolve_root(self.data_dir)),
                        enabled,
                    )
                )
                if enabled and changed:
                    self._live.pop(source_id, None)
                    if self._started:
                        await self.request_scan(source_id)

    async def start(self) -> None:
        if self._started:
            return
        self._started = True
        for source_id, enabled in self._enabled.items():
            if enabled:
                await self.request_scan(source_id)
        self._periodic_task = asyncio.create_task(
            self._periodic(), name="image-studio-external-galleries"
        )

    async def close(self) -> None:
        self._started = False
        if self._periodic_task:
            self._periodic_task.cancel()
            await asyncio.gather(self._periodic_task, return_exceptions=True)
            self._periodic_task = None
        for source_id in self.adapters:
            self._epochs[source_id] += 1
            await self._stop_source(source_id)

    async def _stop_source(self, source_id: str) -> None:
        task = self._tasks.pop(source_id, None)
        if task and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def _periodic(self) -> None:
        while self._started:
            await asyncio.sleep(self.scan_interval)
            for source_id, enabled in self._enabled.items():
                if enabled:
                    await self.request_scan(source_id)

    async def request_scan(self, source_id: str = "nai") -> dict[str, Any]:
        if source_id not in self.adapters:
            raise ValueError("未知的外部图库来源")
        if not self._enabled[source_id]:
            raise ValueError("请先启用该外部图库扫描")
        task = self._tasks.get(source_id)
        if task is None or task.done():
            self._live[source_id] = {
                "status": "enumerating",
                "processed": 0,
                "total": 0,
                "error": "",
                "errors": [],
                "skipped": 0,
            }
            self._tasks[source_id] = asyncio.create_task(
                self._scan(source_id, self._epochs[source_id]),
                name=f"image-studio-scan-{source_id}",
            )
        return {
            "id": source_id,
            "name": self.adapters[source_id].name,
            "enabled": True,
            **self._live.get(source_id, {}),
        }

    async def status(self) -> list[dict[str, Any]]:
        stored = {
            item["id"]: item for item in await self.store.external_sources_status()
        }
        result = []
        for source_id, adapter in self.adapters.items():
            item = {
                "id": source_id,
                "name": adapter.name,
                "enabled": self._enabled[source_id],
                "status": "disabled",
                "processed": 0,
                "total": 0,
                "indexed_count": 0,
                "size_bytes": 0,
                "thumbnail_count": 0,
                "thumbnail_bytes": 0,
                "last_scan_at": 0,
                "error": "",
                "errors": [],
                "skipped": 0,
                **stored.get(source_id, {}),
                **self._live.get(source_id, {}),
            }
            item["enabled"] = self._enabled[source_id]
            if not item["enabled"]:
                item["status"] = "disabled"
            result.append(item)
        return result

    def _current(self, source_id: str, epoch: int) -> bool:
        return self._enabled[source_id] and self._epochs[source_id] == epoch

    async def _scan(self, source_id: str, epoch: int) -> None:
        from .image_metadata import PARSER_VERSION

        adapter = self.adapters[source_id]
        root = adapter.resolve_root(self.data_dir)
        report = self._live[source_id]
        errors: list[str] = []
        error_count = 0
        processed = skipped = 0
        last_update = 0.0

        def add_error(message: str) -> None:
            nonlocal error_count
            error_count += 1
            if len(errors) < _MAX_REPORTED_ERRORS:
                errors.append(message[:500])

        try:
            entries, seen, root_identity, enumeration_errors = await asyncio.to_thread(
                _enumerate_source, adapter, root
            )
            if not self._current(source_id, epoch):
                return
            for error in enumeration_errors:
                add_error(error)
            snapshot = await self.store.external_scan_snapshot(source_id)
            report.update(status="scanning", total=len(seen))
            for entry in entries:
                if not self._current(source_id, epoch):
                    return
                filename, fingerprint, sidecars = entry
                previous = snapshot.get(filename, {})
                serialized = _fingerprint_json(fingerprint)
                sidecar_serialized = _fingerprint_json(sidecars)
                try:
                    if (
                        previous.get("fingerprint") == serialized
                        and previous.get("sidecar_fingerprint") == sidecar_serialized
                        and previous.get("thumbnail_available", True)
                        and previous.get("available", True)
                        and previous.get("thumbnail_max_edge") == self.preview_max_edge
                        and previous.get("thumbnail_quality") == self.preview_quality
                        and int(previous.get("parser_version") or 0) >= PARSER_VERSION
                    ):
                        skipped += 1
                    else:
                        data, parameters, parameter_errors = await asyncio.to_thread(
                            _load_entry, adapter, root, entry
                        )
                        if not self._current(source_id, epoch):
                            return
                        for error in parameter_errors:
                            add_error(error)
                        await _settle_mutation(
                            self.store.upsert_external_image(
                                source_id,
                                filename,
                                data,
                                fingerprint=serialized,
                                # Preserve file identities for explicit deletion;
                                # the marker prevents incremental scans skipping
                                # malformed sidecars that still need a retry.
                                sidecar_fingerprint=(
                                    _fingerprint_json(
                                        {**sidecars, "__parse_error__": True}
                                    )
                                    if parameter_errors
                                    else sidecar_serialized
                                ),
                                size_bytes=fingerprint["size_bytes"],
                                mtime_ns=fingerprint["mtime_ns"],
                                parameters=parameters,
                                generation_engine=adapter.generation_engine,
                                created_at=adapter.created_at(
                                    filename, fingerprint["mtime_ns"]
                                ),
                                preview_max_edge=self.preview_max_edge,
                                preview_quality=self.preview_quality,
                            )
                        )
                except (OSError, ValueError, yaml.YAMLError) as exc:
                    add_error(f"{filename}：{exc}")
                processed += 1
                now = time.monotonic()
                if now - last_update >= 0.1:
                    report.update(processed=processed, skipped=skipped)
                    last_update = now
            if not self._current(source_id, epoch):
                return
            # Directory mtime normally changes while NAI appends files. Its inode
            # must still match; new files simply join the next incremental pass.
            latest_root = await asyncio.to_thread(root.stat)
            if (latest_root.st_dev, latest_root.st_ino) != (
                root_identity["device"],
                root_identity["inode"],
            ):
                add_error("图库文件夹在扫描期间被替换，将在下次扫描重试")
            if not error_count:
                await _settle_mutation(
                    self.store.reconcile_external_source(source_id, seen)
                )
            report.update(
                status="warning" if error_count else "complete",
                processed=len(seen),
                skipped=skipped,
                last_scan_at=time.time(),
                errors=errors,
                error_count=error_count,
                error=f"{error_count} 个文件未能完整读取，将在下次扫描重试"
                if error_count
                else "",
            )
        except asyncio.CancelledError:
            raise
        except (FileNotFoundError, NotADirectoryError) as exc:
            report.update(
                status="unavailable",
                error="未找到 NAI 插件图库目录"
                if source_id == "nai"
                else "未找到外部图库目录",
                errors=[str(exc)[:500]],
                last_scan_at=time.time(),
            )
        except Exception as exc:
            _LOGGER.exception("External gallery scan failed for %s", source_id)
            report.update(
                status="error",
                error=f"扫描失败：{exc}"[:500],
                errors=[str(exc)[:500]],
                last_scan_at=time.time(),
            )
        if self._current(source_id, epoch):
            try:
                await _settle_mutation(
                    self.store.set_external_status(source_id, dict(report))
                )
            except Exception:
                _LOGGER.exception(
                    "Could not persist external gallery scan status for %s", source_id
                )
