"""Bounded, reusable display images; never return full-resolution fallback bytes."""

from __future__ import annotations

import asyncio
import io
import os
from collections import OrderedDict
from collections.abc import Callable, Hashable
from dataclasses import dataclass

from PIL import Image, ImageOps

DISPLAY_EDGES = (768, 1024, 1536, 2048)


def display_max_edge(value: object = 1536) -> int:
    """Round a positive pixel budget up to a bounded, reusable cache bucket."""
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise ValueError("max_edge 必须是 1 至 2048 的整数")
    text = str(value).strip()
    if not text.isascii() or not text.isdecimal() or not 1 <= int(text) <= 2048:
        raise ValueError("max_edge 必须是 1 至 2048 的整数")
    return next(edge for edge in DISPLAY_EDGES if edge >= int(text))


def display_file_version(stat: os.stat_result) -> tuple[int, ...]:
    return (
        stat.st_dev,
        stat.st_ino,
        stat.st_size,
        stat.st_mtime_ns,
        stat.st_ctime_ns,
    )


class DisplayImageError(ValueError):
    """A source exists but cannot produce a safe display image."""


@dataclass(frozen=True)
class DisplayImage:
    data: bytes
    width: int
    height: int


def encode_display_image(reader: Callable[[], bytes], max_edge: int) -> DisplayImage:
    content = reader()
    try:
        with Image.open(io.BytesIO(content)) as original:
            oriented = ImageOps.exif_transpose(original)
            has_alpha = oriented.mode in {"RGBA", "LA", "PA"} or (
                "transparency" in oriented.info
            )
            display = oriented.convert("RGBA" if has_alpha else "RGB")
            display.thumbnail((max_edge, max_edge), Image.Resampling.LANCZOS)
            # Pillow conversions retain info, including EXIF/XMP/ICC profiles.
            # Display assets contain pixels only; downloads retain the source.
            display.info.clear()
            output = io.BytesIO()
            display.save(output, "WEBP", quality=88, method=4)
            return DisplayImage(output.getvalue(), *display.size)
    except (OSError, ValueError, Image.DecompressionBombError) as exc:
        raise DisplayImageError("无法创建图片显示预览，请尝试查看或下载原图") from exc


class DisplayImageCache:
    """Coalesce encodes without letting waiting callers occupy worker threads."""

    def __init__(
        self, *, max_bytes: int = 16 * 1024 * 1024, max_items: int = 48
    ) -> None:
        self.max_bytes = max_bytes
        self.max_items = max_items
        self.size_bytes = 0
        self._entries: OrderedDict[Hashable, DisplayImage] = OrderedDict()
        self._pending: dict[Hashable, asyncio.Task[DisplayImage]] = {}
        self._encode_slots = asyncio.Semaphore(2)

    async def get(
        self, key: Hashable, reader: Callable[[], bytes], max_edge: int
    ) -> DisplayImage:
        cached = self._entries.get(key)
        if cached is not None:
            self._entries.move_to_end(key)
            return cached
        task = self._pending.get(key)
        if task is None:
            task = asyncio.create_task(self._create(key, reader, max_edge))
            self._pending[key] = task
            task.add_done_callback(lambda finished: self._finished(key, finished))
        # One abandoned browser request must not cancel a shared encode or free
        # its slot while its to_thread worker is still decoding.
        return await asyncio.shield(task)

    def _finished(self, key: Hashable, task: asyncio.Task[DisplayImage]) -> None:
        self._pending.pop(key, None)
        if not task.cancelled():
            task.exception()

    async def _create(
        self, key: Hashable, reader: Callable[[], bytes], max_edge: int
    ) -> DisplayImage:
        async with self._encode_slots:
            result = await asyncio.to_thread(encode_display_image, reader, max_edge)
        size = len(result.data)
        if size <= self.max_bytes and self.max_items > 0:
            while self._entries and (
                len(self._entries) >= self.max_items
                or self.size_bytes + size > self.max_bytes
            ):
                _, removed = self._entries.popitem(last=False)
                self.size_bytes -= len(removed.data)
            self._entries[key] = result
            self.size_bytes += size
        return result

    async def close(self) -> None:
        if self._pending:
            await asyncio.gather(*tuple(self._pending.values()), return_exceptions=True)
        self._entries.clear()
        self.size_bytes = 0
