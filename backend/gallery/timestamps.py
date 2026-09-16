"""Creation-time hints for external files, independent of generation parameters."""

from __future__ import annotations

import ctypes
import io
import math
import os
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

from PIL import Image

from ..metadata.parser import MAX_METADATA_BYTES, _creation_timestamp, _decode_comment

TIME_POLICY_VERSION = 1


def _valid_timestamp(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        timestamp = float(value)
    except (ValueError, TypeError, OverflowError):
        return None
    return (
        timestamp
        if math.isfinite(timestamp) and -62135596800 <= timestamp < 253402300800
        else None
    )


def nai_filename_timestamp(filename: str) -> float | None:
    match = re.fullmatch(r"nai_(\d{16,20})\.[^.]+", Path(filename).name)
    return _valid_timestamp(int(match[1]) / 1_000_000_000) if match else None


def _xmp_creation_fields(value: str | bytes) -> dict[str, str]:
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    if len(value.encode("utf-8")) > MAX_METADATA_BYTES or re.search(
        r"<!\s*(?:DOCTYPE|ENTITY)\b", value, re.IGNORECASE
    ):
        return {}
    try:
        root = ET.fromstring(value)
    except (ET.ParseError, ValueError):
        return {}
    result = {}
    accepted = {"CreateDate", "DateCreated", "DateTimeOriginal", "DateTimeDigitized"}
    for element in root.iter():
        for key, text in ((element.tag, element.text), *element.attrib.items()):
            name = key.rsplit("}", 1)[-1].rsplit(":", 1)[-1]
            if name in accepted and isinstance(text, str) and text.strip():
                result.setdefault(name, text.strip())
    return result


def image_creation_timestamp(data: bytes) -> float | None:
    """Read only explicit creation fields; never EXIF DateTime or PNG tIME."""
    fields = {}
    try:
        with Image.open(io.BytesIO(data)) as image:
            if image.format == "PNG":
                fields.update(image.text)
            fields.update(
                {
                    key: value
                    for key, value in image.info.items()
                    if isinstance(value, str)
                }
            )
            for key in ("xmp", "XML:com.adobe.xmp"):
                packet = image.info.get(key) or fields.get(key)
                if isinstance(packet, (str, bytes)):
                    for name, value in _xmp_creation_fields(packet).items():
                        fields.setdefault(name, value)
            exif = image.getexif()
            tags = dict(exif)
            if 34665 in exif:
                tags.update(exif.get_ifd(34665))
            for tag, key in (
                (36867, "DateTimeOriginal"),
                (36868, "DateTimeDigitized"),
                (36881, "OffsetTimeOriginal"),
                (36882, "OffsetTimeDigitized"),
                (37521, "SubSecTimeOriginal"),
                (37522, "SubSecTimeDigitized"),
            ):
                value = tags.get(tag)
                if isinstance(value, (str, bytes)):
                    fields.setdefault(key, _decode_comment(value))
            packet = tags.get(700)
            if isinstance(packet, (str, bytes)):
                for name, value in _xmp_creation_fields(packet).items():
                    fields.setdefault(name, value)
    except (OSError, ValueError, SyntaxError, Image.DecompressionBombError):
        # Image decoding and malformed-generation-metadata reporting belong to
        # storage. A missing creation hint must never prevent ordinary browsing.
        return None
    result = {"normalized": {}, "warnings": []}
    _creation_timestamp(fields, result)
    return _valid_timestamp(result["normalized"].get("generated_at"))


class _StatxTimestamp(ctypes.Structure):
    _fields_ = [
        ("tv_sec", ctypes.c_int64),
        ("tv_nsec", ctypes.c_uint32),
        ("reserved", ctypes.c_int32),
    ]


class _Statx(ctypes.Structure):
    # Linux statx is an ABI-stable 256-byte structure (including reserved tail).
    _fields_ = [
        ("mask", ctypes.c_uint32),
        ("blksize", ctypes.c_uint32),
        ("attributes", ctypes.c_uint64),
        ("nlink", ctypes.c_uint32),
        ("uid", ctypes.c_uint32),
        ("gid", ctypes.c_uint32),
        ("mode", ctypes.c_uint16),
        ("spare0", ctypes.c_uint16),
        ("ino", ctypes.c_uint64),
        ("size", ctypes.c_uint64),
        ("blocks", ctypes.c_uint64),
        ("attributes_mask", ctypes.c_uint64),
        ("atime", _StatxTimestamp),
        ("btime", _StatxTimestamp),
        ("ctime", _StatxTimestamp),
        ("mtime", _StatxTimestamp),
        ("rdev_major", ctypes.c_uint32),
        ("rdev_minor", ctypes.c_uint32),
        ("dev_major", ctypes.c_uint32),
        ("dev_minor", ctypes.c_uint32),
        ("mnt_id", ctypes.c_uint64),
        ("dio_mem_align", ctypes.c_uint32),
        ("dio_offset_align", ctypes.c_uint32),
        ("spare", ctypes.c_uint64 * 12),
    ]


def _statx_function():
    if not sys.platform.startswith("linux") or ctypes.sizeof(_Statx) != 256:
        return None
    try:
        function = ctypes.CDLL(None, use_errno=True).statx
        function.argtypes = [
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_uint,
            ctypes.POINTER(_Statx),
        ]
        function.restype = ctypes.c_int
        return function
    except (AttributeError, OSError):
        return None


_STATX = _statx_function()


def filesystem_birthtime(path: Path, expected: dict[str, int]) -> float | None:
    """Read true birth time, never ctime, and reject a replaced file's timestamp."""
    try:
        current = path.lstat()
        if any(
            (
                current.st_ino != expected["inode"],
                current.st_dev != expected["device"],
                current.st_size != expected["size_bytes"],
                current.st_mtime_ns != expected["mtime_ns"],
                current.st_ctime_ns != expected["ctime_ns"],
            )
        ):
            return None
        birthtime = _valid_timestamp(getattr(current, "st_birthtime", None))
        if birthtime is not None:
            return birthtime
        if _STATX is None:
            return None
        value = _Statx()
        # AT_FDCWD; AT_SYMLINK_NOFOLLOW; STATX_BASIC_STATS | STATX_BTIME.
        if _STATX(-100, os.fsencode(path), 0x100, 0xFFF, ctypes.byref(value)) != 0:
            return None
        if not value.mask & 0x800 or value.btime.tv_nsec >= 1_000_000_000:
            return None
        if any(
            (
                value.ino != expected["inode"],
                value.size != expected["size_bytes"],
                os.makedev(value.dev_major, value.dev_minor) != expected["device"],
                value.mtime.tv_sec * 1_000_000_000 + value.mtime.tv_nsec
                != expected["mtime_ns"],
                value.ctime.tv_sec * 1_000_000_000 + value.ctime.tv_nsec
                != expected["ctime_ns"],
            )
        ):
            return None
        return _valid_timestamp(
            value.btime.tv_sec + value.btime.tv_nsec / 1_000_000_000
        )
    except (OSError, ValueError, OverflowError):
        return None


def external_image_times(
    *, filename: str, data: bytes, path: Path, fingerprint: dict, nai: bool = False
) -> dict:
    embedded = image_creation_timestamp(data)
    birthtime = filesystem_birthtime(path, fingerprint)
    candidates = [
        ("nai_filename", nai_filename_timestamp(filename) if nai else None),
        ("metadata", embedded),
        ("btime", birthtime),
        ("mtime", _valid_timestamp(fingerprint["mtime_ns"] / 1_000_000_000)),
    ]
    selected = next(
        ((key, value) for key, value in candidates if value is not None), None
    )
    if selected is None:
        raise ValueError(f"{filename} 没有可读取的有效文件时间")
    source, timestamp = selected
    return {
        "created_at": timestamp,
        "time_source": source,
        "metadata_created_at": embedded,
        "file_birthtime": birthtime,
        "time_policy_version": TIME_POLICY_VERSION,
    }
