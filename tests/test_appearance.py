from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from astrbot_plugin_image_studio.appearance import (
    APPEARANCE_COOKIE,
    APPEARANCE_DEFAULTS,
    decode_appearance_cookie,
    encode_appearance_cookie,
    normalize_appearance,
)
from astrbot_plugin_image_studio.tests.webui_harness import create_app


@pytest.mark.parametrize("value", [None, [], "dark", 5, True])
def test_appearance_invalid_shapes_use_defaults(value):
    assert normalize_appearance(value) == APPEARANCE_DEFAULTS


def test_appearance_numeric_values_clamped_and_untrusted_strings_ignored():
    assert normalize_appearance(
        {
            "preference": "dark",
            "accentHue": "1); background:url(https://bad.test)",
            "accentSaturation": 1000,
            "glassOpacity": -9,
            "extra": "ignored",
        }
    ) == {
        **APPEARANCE_DEFAULTS,
        "preference": "dark",
        "accentSaturation": 100,
        "glassOpacity": 0.2,
    }
    assert normalize_appearance(
        {"accentHue": 10**500, "accentSaturation": True, "glassOpacity": float("nan")}
    ) == {**APPEARANCE_DEFAULTS, "accentHue": 359.999999}
    assert normalize_appearance({"accentHue": float("inf")}) == APPEARANCE_DEFAULTS


@pytest.mark.parametrize("cookie", [None, "", "invalid", "%7B", "a" * 1025, "%5B%5D"])
def test_appearance_malformed_cookies_are_safe(cookie):
    assert decode_appearance_cookie(cookie) == APPEARANCE_DEFAULTS


def test_appearance_cookie_round_trip():
    value = {
        **APPEARANCE_DEFAULTS,
        "preference": "light",
        "accentHue": 345,
        "accentSaturation": 20,
        "glassOpacity": 0.84,
    }
    encoded = encode_appearance_cookie(value)
    assert ";" not in encoded and '"' not in encoded
    assert decode_appearance_cookie(encoded) == value


def test_appearance_api_is_browser_owned_not_plugin_settings(tmp_path):
    async def run():
        app = await create_app(tmp_path, seed=False)
        settings_before = json.dumps(app.state.plugin._studio_settings, sort_keys=True)
        value = {
            **APPEARANCE_DEFAULTS,
            "preference": "dark",
            "accentHue": 216,
            "accentSaturation": 27,
            "glassOpacity": 0.73,
        }
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as browser:
            initial = await browser.get("/astrbot_plugin_image_studio/appearance")
            assert initial.json() == APPEARANCE_DEFAULTS
            assert initial.headers["cache-control"] == "no-store"
            saved = await browser.post(
                "/astrbot_plugin_image_studio/appearance", json=value
            )
            assert saved.status_code == 200 and saved.json() == value
            cookie = saved.headers["set-cookie"]
            assert f"{APPEARANCE_COOKIE}=" in cookie
            assert (
                "HttpOnly" in cookie and "SameSite=lax" in cookie and "Path=/" in cookie
            )
            assert "Max-Age=31536000" in cookie
            assert (
                await browser.get("/astrbot_plugin_image_studio/appearance")
            ).json() == value
            browser.cookies.clear()
            assert (
                await browser.get("/astrbot_plugin_image_studio/appearance")
            ).json() == APPEARANCE_DEFAULTS
        assert (
            json.dumps(app.state.plugin._studio_settings, sort_keys=True)
            == settings_before
        )
        assert not any("appearance" in path.name for path in tmp_path.rglob("*"))

    asyncio.run(run())


def test_old_appearance_cookie_defaults_source_lightness():
    old = {
        "preference": "dark",
        "accentHue": 216,
        "accentSaturation": 38,
        "glassOpacity": 0.68,
    }
    assert decode_appearance_cookie(encode_appearance_cookie(old)) == {
        **old,
        "accentLightness": 50,
    }


@pytest.mark.parametrize(
    "saturation,lightness,opacity", [(0, 0, 0.2), (100, 100, 1), (87.25, 49.8, 0.31)]
)
def test_extended_appearance_color_range_round_trips(saturation, lightness, opacity):
    value = {
        **APPEARANCE_DEFAULTS,
        "accentSaturation": saturation,
        "accentLightness": lightness,
        "glassOpacity": opacity,
    }
    assert decode_appearance_cookie(encode_appearance_cookie(value)) == value


def test_source_lightness_rejects_untrusted_values():
    for value in ("42", True, float("nan"), float("inf"), None):
        assert normalize_appearance({"accentLightness": value}) == APPEARANCE_DEFAULTS
    assert normalize_appearance({"accentLightness": -1})["accentLightness"] == 0
    assert normalize_appearance({"accentLightness": 101})["accentLightness"] == 100
