"""External gallery endpoints against isolated filesystem data and the real store."""

import asyncio
import base64
import json
from pathlib import Path

import httpx

from astrbot_plugin_image_studio.tests.support.webui_harness import (
    create_app,
    fixture_image,
)

PREFIX = "/astrbot_plugin_image_studio/"


async def completed(plugin):
    task = plugin._external_gallery._tasks.get("nai")
    if task:
        await asyncio.wait_for(asyncio.shield(task), 10)


def test_settings_scan_reference_and_external_delete_confirmation(tmp_path):
    async def run():
        app = await create_app(tmp_path / "studio", seed=False)
        plugin = app.state.plugin
        await plugin._external_gallery.start()
        root = tmp_path / "astrbot_plugin_nai_image" / "image_history"
        root.mkdir(parents=True)
        content = fixture_image(0, novelai=True)
        original = root / "nai_1789000000000000000.png"
        original.write_bytes(content)
        sidecar = original.with_suffix(".yaml")
        sidecar.write_text(
            "tag: external-specific-prompt\nmodel: nai-diffusion-4-5-full\ncfg: 0.3\n",
            encoding="utf-8",
        )
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                status = (await client.get(PREFIX + "external/status")).json()
                assert status["sources"] == []
                assert {item["id"] for item in status["types"]} == {"nai", "directory"}
                assert (
                    await client.post(
                        PREFIX + "external/scan", json={"source_id": "nai"}
                    )
                ).status_code == 400
                settings = (await client.get(PREFIX + "settings/get")).json()
                settings["webui"]["external_sources"] = {"nai": {"enabled": True}}
                saved = await client.post(
                    PREFIX + "settings/save",
                    json={
                        "webui": settings["webui"],
                        "base": settings["base"],
                        "settings_revision": settings["webui"]["revision"],
                    },
                )
                assert saved.status_code == 200, saved.text
                await completed(plugin)
                status = (await client.get(PREFIX + "external/status")).json()[
                    "sources"
                ][0]
                assert status["status"] == "complete", status
                assert status["indexed_count"] == 1 and status["thumbnail_count"] == 1
                gallery = (
                    await client.get(
                        PREFIX + "gallery/list",
                        params={"query": "external-specific-prompt"},
                    )
                ).json()
                assert gallery["total"] == 1
                record = gallery["items"][0]
                assert (
                    record["is_external"] and record["external_source"]["id"] == "nai"
                )
                assert record["generation_engine"] == "novelai"
                detail = (
                    await client.get(PREFIX + "gallery/detail/" + record["id"])
                ).json()
                image_id = detail["images"][0]["id"]
                exported = await client.get(
                    PREFIX + "gallery/parameters/" + record["id"]
                )
                envelope = json.loads(exported.json()["content"])
                assert envelope["has_request_snapshot"] is False
                assert envelope["metadata"]["normalized"]["seed"] == 100
                assert envelope["data"]["prompt"] == "external-specific-prompt"
                assert envelope["data"]["negative_prompt"] == "low quality"
                assert envelope["data"]["parameters"]["cfg"] == 0.3
                nai_export = await client.get(
                    PREFIX + "gallery/parameters/" + record["id"],
                    params={"format": "nai"},
                )
                assert json.loads(nai_export.json()["content"])["cfg"] == 0.3
                reproduced = await client.post(
                    PREFIX + "gallery/reproduce/" + record["id"],
                    json={"image_id": image_id},
                )
                assert reproduced.status_code == 200, reproduced.text
                assert reproduced.json()["prompt"] == "external-specific-prompt"
                assert reproduced.json()["negative_prompt"] == "low quality"
                original_response = await client.get(
                    PREFIX + "gallery/image/" + image_id, params={"detail": "original"}
                )
                assert original_response.status_code == 200
                assert (
                    base64.b64decode(
                        original_response.json()["data_url"].split(",", 1)[1]
                    )
                    == content
                )
                reference = await client.post(
                    PREFIX + "studio/reference/from-gallery",
                    json={"image_id": image_id},
                )
                assert reference.status_code == 200, reference.text
                health = (await client.get(PREFIX + "storage/health")).json()
                assert health["external_sources"][0]["size_bytes"] == len(content)
                preview = await client.post(
                    PREFIX + "gallery/delete/preview", json={"ids": [record["id"]]}
                )
                assert preview.json()["external_count"] == 1
                denied = await client.post(
                    PREFIX + "gallery/images/delete",
                    json={"generation_id": record["id"], "image_ids": [image_id]},
                )
                assert denied.status_code == 409 and original.is_file()
                denied = await client.post(
                    PREFIX + "gallery/delete", json={"ids": [record["id"]]}
                )
                assert denied.status_code == 409 and original.is_file()
                deleted = await client.post(
                    PREFIX + "gallery/delete",
                    json={"ids": [record["id"]], "confirm_external": True},
                )
                assert deleted.status_code == 200 and deleted.json()["deleted"] == [
                    record["id"]
                ], deleted.text
                assert not original.exists() and not sidecar.exists()
                assert (await client.get(PREFIX + "gallery/list")).json()["total"] == 0
                refs = await plugin.store.load_staged_references(
                    [reference.json()["id"]]
                )
                assert refs[0].data == content
                assert not list(plugin.store.assets_dir.rglob("*.png"))
        finally:
            await plugin._external_gallery.close()
            await plugin.store.close()

    asyncio.run(run())


def test_scan_missing_source_and_invalid_delete_selection_are_reported(tmp_path):
    async def run():
        app = await create_app(tmp_path / "studio", seed=False)
        plugin = app.state.plugin
        try:
            await plugin._external_gallery.configure({"nai": {"enabled": True}})
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                result = await client.post(
                    PREFIX + "external/scan", json={"source_id": "nai"}
                )
                assert result.status_code == 200
                await completed(plugin)
                status = (await client.get(PREFIX + "external/status")).json()[
                    "sources"
                ][0]
                assert status["status"] == "unavailable" and status["error"]
                assert not (tmp_path / "astrbot_plugin_nai_image").exists()
                for selection in ([], ["../not-an-id"], [None], ["a" * 32] * 201):
                    result = await client.post(
                        PREFIX + "gallery/delete/preview", json={"ids": selection}
                    )
                    assert result.status_code == 400, result.text
                assert (
                    await client.post(
                        PREFIX + "external/scan", json={"source_id": "missing"}
                    )
                ).status_code == 400
        finally:
            await plugin._external_gallery.close()
            await plugin.store.close()

    asyncio.run(run())


def test_partial_sidecar_failure_reports_deleted_record_with_warning(
    tmp_path, monkeypatch
):
    async def run():
        app = await create_app(tmp_path / "studio", seed=False)
        plugin = app.state.plugin
        root = tmp_path / "astrbot_plugin_nai_image" / "image_history"
        root.mkdir(parents=True)
        original = root / "nai_1789000000000000000.png"
        original.write_bytes(fixture_image(0, novelai=True))
        sidecar = original.with_suffix(".yaml")
        sidecar.write_text("cfg: 0.3\n", encoding="utf-8")
        try:
            await plugin._external_gallery.configure({"nai": {"enabled": True}})
            await plugin._external_gallery.request_scan("nai")
            await completed(plugin)
            record = (await plugin.store.list_generations({}))["items"][0]
            unlink = Path.unlink

            def fail_sidecar(path, *args, **kwargs):
                if path == sidecar:
                    raise PermissionError("test sidecar cannot be removed")
                return unlink(path, *args, **kwargs)

            monkeypatch.setattr(Path, "unlink", fail_sidecar)
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                result = await client.post(
                    PREFIX + "gallery/delete",
                    json={"ids": [record["id"]], "confirm_external": True},
                )
                assert result.status_code == 200, result.text
                assert result.json()["deleted"] == [record["id"]]
                assert result.json()["failed"] == []
                assert result.json()["errors"][0]["generation_deleted"] is True
                assert "参数文件" in result.json()["errors"][0]["message"]
                assert not original.exists() and sidecar.exists()
        finally:
            await plugin._external_gallery.close()
            await plugin.store.close()

    asyncio.run(run())


def test_saved_settings_revision_survives_external_configuration_failure(
    tmp_path, monkeypatch
):
    async def run():
        app = await create_app(tmp_path / "studio", seed=False)
        plugin = app.state.plugin

        async def fail():
            raise OSError("simulated unavailable external index")

        monkeypatch.setattr(plugin, "_configure_external_gallery", fail)
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                settings = (await client.get(PREFIX + "settings/get")).json()["webui"]
                settings["external_sources"]["nai"] = {"enabled": True}
                result = await client.post(
                    PREFIX + "settings/save",
                    json={"settings_revision": settings["revision"], "webui": settings},
                )
                assert result.status_code == 200
                assert result.json()["settings_revision"] == settings["revision"] + 1
                assert result.json()["warnings"]
                persisted = (await client.get(PREFIX + "settings/get")).json()["webui"]
                assert persisted["external_sources"]["nai"]["enabled"] is True
        finally:
            await plugin._external_gallery.close()
            await plugin.store.close()

    asyncio.run(run())
