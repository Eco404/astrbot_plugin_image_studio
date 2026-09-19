"""Configured external collections and operation policies through real Page APIs."""

import asyncio
import os

import httpx

from astrbot_plugin_image_studio.backend.config import normalize_webui_settings
from astrbot_plugin_image_studio.tests.support.webui_harness import (
    create_app,
    fixture_image,
)

PREFIX = "/astrbot_plugin_image_studio/"


async def save(client, sources):
    settings = (await client.get(PREFIX + "settings/get")).json()["webui"]
    settings["external_sources"] = sources
    return await client.post(
        PREFIX + "settings/save",
        json={"settings_revision": settings["revision"], "webui": settings},
    )


async def finish(plugin):
    await asyncio.wait_for(
        asyncio.gather(*list(plugin._external_gallery._tasks.values())), 10
    )


def test_legacy_normalization_and_new_source_defaults():
    empty, errors = normalize_webui_settings({})
    assert not errors and empty["external_sources"] == {}
    legacy, errors = normalize_webui_settings(
        {"external_sources": {"nai": {"enabled": True}}}
    )
    assert not errors
    assert legacy["external_sources"]["nai"] == {
        "type": "nai",
        "name": "nai-image 插件图库",
        "enabled": True,
        "path": "",
        "recursive": False,
        "permissions": {
            "favorite": True,
            "delete": True,
            "download": True,
            "reference": True,
        },
    }
    custom, errors = normalize_webui_settings(
        {
            "external_sources": {
                "album": {"type": "directory", "name": "照片", "path": "/mnt/photos"}
            }
        }
    )
    assert (
        not errors
        and custom["external_sources"]["album"]["permissions"]["delete"] is False
    )
    for entry in (
        {"type": []},
        {"type": "directory", "path": "relative"},
        {"type": "directory", "path": "/mnt/photos", "permissions": []},
    ):
        assert normalize_webui_settings({"external_sources": {"album": entry}})[1]


def test_action_preflight_preserves_existing_favorite_selection_limit(tmp_path):
    async def run():
        app = await create_app(tmp_path / "studio", seed=False)
        plugin = app.state.plugin
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                ids = [f"{value:032x}" for value in range(201)]
                favorite = await client.post(
                    PREFIX + "gallery/delete/preview",
                    json={"ids": ids, "action": "favorite"},
                )
                assert favorite.status_code == 200 and favorite.json()["allowed"]
                assert (
                    await client.post(
                        PREFIX + "gallery/delete/preview",
                        json={"ids": ids, "action": "delete"},
                    )
                ).status_code == 400
        finally:
            await plugin._external_gallery.close()
            await plugin.store.close()

    asyncio.run(run())


def test_custom_plain_gallery_crud_and_permissions_preserve_files(
    tmp_path, monkeypatch
):
    from astrbot_plugin_image_studio.backend.gallery import (
        timestamps as external_timestamps,
    )

    monkeypatch.setattr(
        external_timestamps, "filesystem_birthtime", lambda *_args: None
    )

    async def run():
        app = await create_app(tmp_path / "studio", seed=False)
        plugin = app.state.plugin
        await plugin._external_gallery.start()
        photos = tmp_path / "photos"
        photos.mkdir()
        original = photos / "plain.png"
        original.write_bytes(fixture_image(20))
        os.utime(original, (1_700_000_000, 1_700_000_000))
        sidecar = photos / "plain.json"
        sidecar.write_text(
            '{"prompt":"must never parse unrelated data"}', encoding="utf-8"
        )
        sources = {
            "album": {
                "type": "directory",
                "name": "普通图片",
                "path": str(photos),
                "enabled": True,
            }
        }
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                response = await save(client, sources)
                assert response.status_code == 200, response.text
                await finish(plugin)
                status = (await client.get(PREFIX + "external/status")).json()[
                    "sources"
                ][0]
                assert status["status"] == "complete" and status["indexed_count"] == 1
                assert status["type"] == "directory" and status["name"] == "普通图片"
                record = (await client.get(PREFIX + "gallery/list")).json()["items"][0]
                gid, iid = record["id"], record["image_id"]
                assert record["generation_engine"] == "unknown"
                assert (
                    record["created_at"] == 1_700_000_000
                    and record["generated_at"] is None
                )
                assert record["time_source"] == "mtime"
                assert not record["allowed_actions"]["delete"]
                detail = (await client.get(PREFIX + "gallery/detail/" + gid)).json()
                assert detail["original_prompt"] == "" and detail["model"] == ""
                reproduce = await client.post(
                    PREFIX + "gallery/reproduce/" + gid, json={}
                )
                assert (
                    reproduce.status_code == 404
                    and "没有可恢复" in reproduce.json()["message"]
                )
                assert (
                    await client.post(
                        PREFIX + "gallery/delete",
                        json={"ids": [gid], "confirm_external": True},
                    )
                ).status_code == 403
                assert (
                    await client.post(
                        PREFIX + "gallery/favorite",
                        json={"generation_id": gid, "favorite": True},
                    )
                ).status_code == 200
                staged = await client.post(
                    PREFIX + "studio/reference/from-gallery", json={"image_id": iid}
                )
                assert staged.status_code == 200
                local = await plugin.store.import_image(
                    fixture_image(22), "local.png", {}
                )
                sources["album"]["name"] = "已重命名"
                sources["album"]["permissions"] = {
                    key: False
                    for key in ("favorite", "delete", "download", "reference")
                }
                assert (await save(client, sources)).status_code == 200
                after = (await client.get(PREFIX + "gallery/detail/" + gid)).json()
                assert after["is_favorite"] is True and after["images"][0]["id"] == iid
                assert after["external_source"]["name"] == "已重命名"
                assert (
                    await client.get(
                        PREFIX + "gallery/image/" + iid, params={"detail": "original"}
                    )
                ).status_code == 200
                for endpoint, body in (
                    (
                        "gallery/favorite",
                        {
                            "generation_ids": [local["generation_id"], gid],
                            "action": "toggle",
                        },
                    ),
                    ("gallery/export", {"ids": [local["generation_id"], gid]}),
                    (
                        "gallery/delete",
                        {
                            "ids": [local["generation_id"], gid],
                            "confirm_external": True,
                        },
                    ),
                    ("studio/reference/from-gallery", {"image_id": iid}),
                ):
                    rejected = await client.post(PREFIX + endpoint, json=body)
                    assert rejected.status_code == 403, rejected.text
                assert (
                    await client.get(PREFIX + "gallery/download/" + iid)
                ).status_code == 403
                preview = (
                    await client.post(
                        PREFIX + "gallery/delete/preview",
                        json={
                            "ids": [local["generation_id"], gid],
                            "action": "favorite",
                        },
                    )
                ).json()
                assert (
                    not preview["allowed"]
                    and preview["denied"][0]["source_name"] == "已重命名"
                )
                assert (await plugin.store.generation_detail(local["generation_id"]))[
                    "is_favorite"
                ] is False
                assert (await save(client, {})).status_code == 200
                assert (await client.get(PREFIX + "external/status")).json()[
                    "sources"
                ] == []
                assert (await client.get(PREFIX + "gallery/list")).json()["total"] == 1
                assert original.is_file() and sidecar.is_file()
                assert (
                    await plugin.store.load_staged_references([staged.json()["id"]])
                )[0].data == original.read_bytes()
                assert (
                    await client.get(PREFIX + "gallery/detail/" + gid)
                ).status_code == 404
        finally:
            await plugin._external_gallery.close()
            await plugin.store.close()

    asyncio.run(run())


def test_overlapping_and_owned_roots_rejected_before_settings_change(tmp_path):
    async def run():
        app = await create_app(tmp_path / "studio", seed=False)
        plugin = app.state.plugin
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                base = {
                    "type": "directory",
                    "name": "照片",
                    "path": str(tmp_path / "photos"),
                    "enabled": True,
                    "recursive": True,
                }
                for sources in (
                    {
                        "a": base,
                        "b": {**base, "path": str(tmp_path / "photos" / "nested")},
                    },
                    {"a": {**base, "path": str(plugin.store.assets_dir)}},
                    {"a": {**base, "path": "/"}},
                ):
                    result = await save(client, sources)
                    assert result.status_code == 400
                    assert (await client.get(PREFIX + "settings/get")).json()["webui"][
                        "revision"
                    ] == 1
                    assert not await plugin.store.external_sources_status()
                offline = await save(client, {"a": base})
                assert offline.status_code == 200
                await plugin._external_gallery.request_scan("a")
                await finish(plugin)
                assert (await client.get(PREFIX + "external/status")).json()["sources"][
                    0
                ]["status"] == "unavailable"
        finally:
            await plugin._external_gallery.close()
            await plugin.store.close()

    asyncio.run(run())
