"""Owned ComfyUI file validation, content-addressed writes and legacy staging."""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import uuid
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from ...models import ReferenceImage
from .job_types import _SAFE_ID, _job_id


class ComfyJobFiles:
    """Validate and operate on files; the job store decides ownership and expiry.

    No method opens a database connection or decides whether a job is recoverable.
    Callers retain their transaction while replacing a manifest or collecting blobs.
    """

    def __init__(self, data_dir: Path):
        self.data_dir = data_dir
        self.inputs_dir = data_dir / "comfyui_inputs"
        self.outputs_dir = data_dir / "comfyui_outputs"
        self.blobs_dir = data_dir / "comfyui_blobs"

    def owned_root(self, root):
        if root.is_symlink() or root.resolve() != self.data_dir.resolve() / root.name:
            raise ValueError("ComfyUI 缓存目录不能指向插件数据目录之外")
        return root

    @staticmethod
    def write_blob(destination, data):
        if destination.is_symlink():
            raise ValueError("ComfyUI 共享图片缓存不能是符号链接")
        if destination.exists():
            if destination.stat().st_size != len(data):
                raise ValueError("ComfyUI 共享图片缓存大小不一致")
            if hashlib.sha256(destination.read_bytes()).hexdigest() != destination.stem:
                raise ValueError("ComfyUI 共享图片缓存内容不一致")
            return
        temporary = destination.parent / f"{uuid.uuid4().hex}.tmp"
        try:
            with temporary.open("xb") as handle:
                handle.write(data)
            temporary.chmod(0o600)
            temporary.replace(destination)
        finally:
            temporary.unlink(missing_ok=True)

    def load_references(
        self, job_id: str, references: Sequence[dict[str, Any]]
    ) -> tuple[ReferenceImage, ...]:
        images = []
        for metadata in references:
            if metadata.get("storage") == "expired":
                raise ValueError(
                    f"ComfyUI 任务参考图不可用：{metadata['filename']}（已过期）"
                )
            blob = metadata.get("storage") == "blob"
            self.owned_root(self.blobs_dir if blob else self.inputs_dir)
            if not blob and (self.inputs_dir / job_id).is_symlink():
                raise ValueError("ComfyUI 任务参考图路径无效")
            directory = (self.blobs_dir if blob else self.inputs_dir / job_id).resolve()
            path = (
                (self.blobs_dir if blob else self.inputs_dir) / metadata["path"]
            ).resolve()
            if path.parent != directory:
                raise ValueError("ComfyUI 任务参考图路径无效")
            try:
                data = path.read_bytes()
            except OSError as exc:
                raise ValueError(
                    f"ComfyUI 任务参考图不可用：{metadata['filename']}"
                ) from exc
            if (
                len(data) != metadata["size_bytes"]
                or hashlib.sha256(data).hexdigest() != metadata["sha256"]
            ):
                raise ValueError(
                    f"ComfyUI 任务参考图内容发生变化：{metadata['filename']}"
                )
            images.append(
                ReferenceImage(
                    metadata["id"], metadata["filename"], data, metadata["mime_type"]
                )
            )
        return tuple(images)

    def output_path(self, job_id: str, item: dict[str, Any]) -> Path:
        directory = (
            self.blobs_dir
            if item.get("storage") == "blob"
            else self.outputs_dir / job_id
        ).resolve()
        self.owned_root(
            self.blobs_dir if item.get("storage") == "blob" else self.outputs_dir
        )
        if item.get("storage") != "blob" and (self.outputs_dir / job_id).is_symlink():
            raise ValueError("ComfyUI 输出图片路径无效")
        path = (
            self.blobs_dir / item["path"]
            if item.get("storage") == "blob"
            else self.outputs_dir / item["path"]
        ).resolve()
        if path.parent != directory:
            raise ValueError("ComfyUI 输出图片路径无效")
        return path

    def remove_unreferenced_blobs(
        self, retained: set[str], temporary_before: float
    ) -> int:
        removed = 0
        if self.owned_root(self.blobs_dir).is_dir():
            for path in self.blobs_dir.iterdir():
                stale_temporary = (
                    (
                        re.fullmatch(r"[0-9a-f]{32}\.tmp", path.name)
                        and path.stat().st_mtime < temporary_before
                    )
                    if not path.is_symlink() and path.is_file()
                    else False
                )
                if (
                    not path.is_symlink()
                    and path.is_file()
                    and (
                        stale_temporary
                        or (
                            re.fullmatch(r"[0-9a-f]{64}\.image", path.name)
                            and path.name not in retained
                        )
                    )
                ):
                    path.unlink(missing_ok=True)
                    removed += 1
        return removed

    def stage_legacy_blob(
        self, job_id: str, root: Path, item: dict[str, Any]
    ) -> tuple[Path, Path] | None:
        source = root / item["path"]
        if source.is_symlink() or (root / _job_id(job_id)).is_symlink():
            raise ValueError("ComfyUI 旧缓存路径不能是符号链接")
        old = source.resolve()
        if old.parent != (root / _job_id(job_id)).resolve():
            raise ValueError("ComfyUI 旧缓存路径无效")
        if not old.is_file():
            return None
        data = old.read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        if digest != item["sha256"] or len(data) != item["size_bytes"]:
            # Corrupt inputs remain visible as a read error; do
            # not certify or delete them while migrating.
            return None
        self.owned_root(self.blobs_dir).mkdir(mode=0o700, parents=True, exist_ok=True)
        destination = self.blobs_dir / f"{digest}.image"
        if destination.is_symlink():
            raise ValueError("ComfyUI 共享图片缓存不能是符号链接")
        if not destination.exists():
            try:
                os.link(old, destination)
            except OSError:
                self.write_blob(destination, data)
        else:
            self.write_blob(destination, data)
        return old, destination

    def remove_terminal_directories(
        self, archived: set[str], known_ids: set[str], terminal_before: float
    ) -> int:
        removed = 0
        for root in (self.inputs_dir, self.outputs_dir):
            if not self.owned_root(root).is_dir():
                continue
            for directory in root.iterdir():
                if (
                    directory.is_dir()
                    and not directory.is_symlink()
                    and _SAFE_ID.fullmatch(directory.name)
                    and (
                        directory.name in archived
                        or (
                            directory.name not in known_ids
                            and directory.stat().st_mtime < terminal_before
                        )
                    )
                ):
                    shutil.rmtree(directory)
                    removed += 1
        return removed
