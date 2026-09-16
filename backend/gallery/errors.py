"""Structured gallery import and external-file failures."""

from __future__ import annotations

from typing import Any


class ImportDuplicateError(ValueError):
    """A recoverable import conflict identified by exact content hashes."""

    def __init__(self, duplicate_hashes: list[str], *, batch: bool = False) -> None:
        self.duplicate_hashes = list(dict.fromkeys(duplicate_hashes))
        self.code = "batch_duplicates" if batch else "gallery_duplicates"
        message = (
            f"本次上传包含 {len(duplicate_hashes)} 张批内重复图片，整批上传已取消，请移除重复项"
            if batch
            else f"本次上传包含 {len(duplicate_hashes)} 张画廊已有图片，整批上传已取消，请移除重复项"
        )
        super().__init__(message)

    def as_dict(self) -> dict[str, Any]:
        return {
            "allowed": False,
            "code": self.code,
            "duplicate_hashes": self.duplicate_hashes,
            "message": str(self),
        }


class ImportEditConflictError(ValueError):
    """The imported record changed after an editor read its snapshot."""


class ExternalPermissionError(ValueError):
    """At least one selected source forbids the requested gallery operation."""

    def __init__(self, denied: list[dict[str, Any]]):
        self.denied = denied
        super().__init__("；".join(item["message"] for item in denied))


class ExternalDeleteError(ValueError):
    """An explicit external deletion failed, possibly after removing some files."""

    def __init__(self, generation_id: str, message: str, *, deleted_files=None):
        super().__init__(message)
        self.generation_id = generation_id
        self.deleted_files = deleted_files or []

    def as_dict(self) -> dict[str, Any]:
        return {
            "generation_id": self.generation_id,
            "code": "external_delete_failed",
            "message": str(self),
            "partial": bool(self.deleted_files),
            "generation_deleted": bool(self.deleted_files),
            "deleted_files": self.deleted_files,
        }


class _ExternalFileChangedError(ValueError):
    """A verified file identity changed, rather than a path or permission error."""
