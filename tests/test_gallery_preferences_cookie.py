"""Browser-owned preference persistence without access to iframe localStorage."""

import asyncio
import base64
import json
import zlib

import httpx
import pytest

from astrbot_plugin_image_studio.backend.ui.gallery_preferences import (
    GALLERY_PREFERENCES_COOKIE,
    decode_gallery_preferences,
    encode_gallery_preferences,
)
from astrbot_plugin_image_studio.tests.webui_harness import create_app

PATH = "/astrbot_plugin_image_studio/gallery/preferences"


@pytest.mark.parametrize(
    "cookie",
    [
        None,
        "",
        "no",
        "1.!invalid",
        "1." + "x" * 3601,
        "1." + base64.urlsafe_b64encode(zlib.compress(b"a" * 65537)).decode(),
    ],
)
def test_invalid_or_oversized_cookie_falls_back_safely(cookie):
    assert decode_gallery_preferences(cookie) == {"sort": "created", "filters": {}}


def test_cookie_roundtrip_preserves_all_intent_and_unspecified_provider():
    value = {
        "sort": "latest_content",
        "filters": {
            "galleryProvider": {"mode": "values", "values": ["", "服务商"]},
            "gallerySource": {"mode": "all"},
        },
    }
    encoded = encode_gallery_preferences(value)
    assert ";" not in encoded and "\n" not in encoded
    assert decode_gallery_preferences(encoded) == value


def test_preferences_cookie_patch_isolated_per_browser_without_config_or_database_writes(
    tmp_path,
):
    async def run():
        app = await create_app(tmp_path, seed=False)
        plugin = app.state.plugin
        before_settings = json.dumps(plugin._studio_settings, sort_keys=True)
        before_revision = await plugin.store.gallery_revision()
        try:
            async with (
                httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app), base_url="http://test"
                ) as first,
                httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app), base_url="http://test"
                ) as second,
            ):
                assert (await first.get(PATH)).json() == {
                    "sort": "created",
                    "filters": {},
                }
                patch = {
                    "filters": {
                        "galleryProvider": {"mode": "values", "values": ["nai"]}
                    }
                }
                response = await first.post(PATH, json=patch)
                assert response.status_code == 200
                assert response.headers["cache-control"] == "no-store"
                assert (
                    f"{GALLERY_PREFERENCES_COOKIE}=" in response.headers["set-cookie"]
                )
                assert all(
                    item in response.headers["set-cookie"]
                    for item in (
                        "HttpOnly",
                        "SameSite=lax",
                        "Path=/",
                        "Max-Age=31536000",
                    )
                )
                sort = await first.post(PATH, json={"sort": "latest_content"})
                assert sort.json() == {"sort": "latest_content", **patch}
                for invalid in (
                    [],
                    {"sort": []},
                    {"sort": "no"},
                    {"filters": {"galleryProvider": {"mode": "values", "values": []}}},
                    {"filters": {"unknown": {"mode": "all"}}},
                    {"filters": []},
                    {"config": {}},
                ):
                    rejected = await first.post(PATH, json=invalid)
                    assert rejected.status_code == 400
                    assert "set-cookie" not in rejected.headers
                assert (await first.get(PATH)).json() == sort.json()
                assert (await second.get(PATH)).json() == {
                    "sort": "created",
                    "filters": {},
                }
                all_selected = await first.post(
                    PATH, json={"filters": {"galleryProvider": {"mode": "all"}}}
                )
                assert all_selected.json()["filters"]["galleryProvider"] == {
                    "mode": "all"
                }
            assert (
                json.dumps(plugin._studio_settings, sort_keys=True) == before_settings
            )
            assert await plugin.store.gallery_revision() == before_revision
        finally:
            await plugin._external_gallery.close()
            await plugin.store.close()

    asyncio.run(run())
