"""Shared gallery validation and retention constants."""

import re

_SAFE_ID_RE = re.compile(r"^[a-f0-9]{32}$")
_SHA256_RE = re.compile(r"^[a-f0-9]{64}$")
_GALLERY_RETENTION_CACHE_SECONDS = 3.0
WORKFLOW_ASSET_RETENTION_SECONDS = 3600
_EXTERNAL_ACTIONS = ("favorite", "delete", "download", "reference")
_EXTERNAL_ACTION_LABELS = {
    "favorite": "修改收藏",
    "delete": "删除原图",
    "download": "下载或导出原图",
    "reference": "用作参考图",
}
_THUMBNAIL_REVISION_SQL = (
    "COALESCE(t.max_edge, 0) || ':' || COALESCE(t.quality, 0) || ':' || "
    "COALESCE(t.size_bytes, 0)"
)
