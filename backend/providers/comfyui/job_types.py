"""Shared job states, identifiers and credential-free snapshot serialization."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any

RECOVERY_SECONDS = 24 * 3600

TERMINAL_STATUSES = frozenset(
    {"succeeded", "partial", "failed", "cancelled", "unknown"}
)
JOB_STATUSES = TERMINAL_STATUSES | {
    "queued",
    "preparing",
    "submitting",
    "submitted",
    "running",
    "downloading",
    "finalizing",
}
_SAFE_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_SECRET_KEYS = frozenset(
    {
        "api_key",
        "apikey",
        "authorization",
        "proxy_authorization",
        "password",
        "access_token",
        "refresh_token",
        "client_secret",
        "auth_headers",
    }
)


def _json_snapshot(value: Any) -> str:
    def check(item: Any) -> None:
        if isinstance(item, Mapping):
            for key, child in item.items():
                if str(key).lower().replace("-", "_") in _SECRET_KEYS and child:
                    raise ValueError("ComfyUI 任务快照不能包含密钥或认证信息")
                check(child)
        elif isinstance(item, (tuple, list)):
            for child in item:
                check(child)

    check(value)
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _job_id(value: str) -> str:
    if not _SAFE_ID.fullmatch(value):
        raise ValueError("ComfyUI 任务编号无效")
    return value
