"""Browser-scoped preference cookies, independent of plugin runtime state."""

from typing import Any

from astrbot.api.web import json_response, error_response
from astrbot.api.web import request as web_request
from ..ui.appearance import (
    APPEARANCE_COOKIE,
    decode_appearance_cookie,
    encode_appearance_cookie,
    normalize_appearance,
)
from ..ui.gallery_preferences import (
    GALLERY_PREFERENCES_COOKIE,
    decode_gallery_preferences,
    encode_gallery_preferences,
    merge_gallery_preferences,
)


async def _api_get_appearance() -> Any:
    response = json_response(
        decode_appearance_cookie(web_request.cookies.get(APPEARANCE_COOKIE))
    )
    response.headers["Cache-Control"] = "no-store"
    return response


async def _api_set_appearance() -> Any:
    settings = normalize_appearance(await web_request.json(default={}))
    response = json_response(settings)
    response.headers["Cache-Control"] = "no-store"
    # The host prefixes plugin API URLs; a namespaced root cookie also works
    # behind a reverse proxy without granting the opaque iframe cookie access.
    response.set_cookie(
        APPEARANCE_COOKIE,
        encode_appearance_cookie(settings),
        max_age=365 * 24 * 60 * 60,
        httponly=True,
        samesite="lax",
        path="/",
    )
    return response


async def _api_get_gallery_preferences() -> Any:
    value = decode_gallery_preferences(
        web_request.cookies.get(GALLERY_PREFERENCES_COOKIE)
    )
    response = json_response(value)
    response.headers["Cache-Control"] = "no-store"
    return response


async def _api_set_gallery_preferences() -> Any:
    try:
        current = decode_gallery_preferences(
            web_request.cookies.get(GALLERY_PREFERENCES_COOKIE)
        )
        settings = merge_gallery_preferences(
            current, await web_request.json(default={})
        )
        encoded = encode_gallery_preferences(settings)
    except (ValueError, TypeError, RecursionError) as exc:
        return error_response(str(exc), status_code=400)
    response = json_response(settings)
    response.headers["Cache-Control"] = "no-store"
    response.set_cookie(
        GALLERY_PREFERENCES_COOKIE,
        encoded,
        max_age=365 * 24 * 60 * 60,
        httponly=True,
        samesite="lax",
        path="/",
    )
    return response
