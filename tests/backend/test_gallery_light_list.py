from __future__ import annotations

import asyncio
from pathlib import Path

import httpx

from astrbot_plugin_image_studio.tests.backend.test_external_storage import (
    add as add_external,
)
from astrbot_plugin_image_studio.tests.backend.test_external_storage import (
    setup as setup_external,
)
from astrbot_plugin_image_studio.tests.backend.test_gallery_api import PREFIX
from astrbot_plugin_image_studio.tests.backend.test_gallery_lazy import (
    track_image_reads,
)
from astrbot_plugin_image_studio.tests.support.webui_harness import create_app


def without_thumbnails(payload):
    return {
        **payload,
        "lightweight": True,
        "items": [
            {key: value for key, value in item.items() if key != "thumbnail_data_url"}
            for item in payload["items"]
        ],
    }


def test_gallery_light_list_preserves_pagination_filters_and_retention(
    tmp_path, monkeypatch
):
    async def run():
        app = await create_app(tmp_path)
        store = app.state.plugin.store
        await store.set_favorite(app.state.seed_ids[0], True)
        reads = track_image_reads(monkeypatch, store)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            for params in (
                {"limit": 24, "offset": 0},
                {"limit": 24, "offset": 24},
                {"limit": 12, "sort": "latest_content"},
                {"favorite": 1},
                {"sources": '["webui", "command"]', "query": "湖泊"},
                {"query": "no matching pictures"},
            ):
                legacy_response = await client.get(
                    PREFIX + "gallery/list", params=params
                )
                assert legacy_response.status_code == 200
                legacy = legacy_response.json()
                assert "lightweight" not in legacy
                assert all(
                    item["thumbnail_data_url"].startswith("data:image/")
                    for item in legacy["items"]
                )
                reads.clear()
                light_response = await client.get(
                    PREFIX + "gallery/list", params={**params, "light": 1}
                )
                assert light_response.status_code == 200
                assert light_response.json() == without_thumbnails(legacy)
                assert reads == []
                if legacy["items"]:
                    assert len(light_response.content) < len(legacy_response.content)

            # The deferred request supplies precisely the bytes the original
            # list embedded, including the same revision for cache identity.
            legacy = (
                await client.get(PREFIX + "gallery/list", params={"limit": 1})
            ).json()
            first = legacy["items"][0]
            reads.clear()
            preview = await client.get(
                PREFIX + "gallery/image/" + first["image_id"],
                params={"detail": "preview"},
            )
            assert preview.status_code == 200
            assert preview.json()["data_url"] == first["thumbnail_data_url"]
            assert preview.json()["thumbnail_revision"] == first["thumbnail_revision"]
            assert len(reads) == 1

            # Compatibility is opt-in, including explicit false values.
            for value in ("0", "false", "no"):
                payload = (
                    await client.get(
                        PREFIX + "gallery/list", params={"limit": 1, "light": value}
                    )
                ).json()
                assert payload == legacy

        await store.close()

    asyncio.run(run())


def test_gallery_light_list_preserves_external_permissions_and_deferred_failure(
    tmp_path, monkeypatch
):
    async def run():
        store, root = await setup_external(tmp_path)
        external, original = await add_external(store, root)
        await store.configure_external_source(
            "nai", "NAI 插件图库", root, True, permissions={"delete": False}
        )
        await store.set_favorite(external["generation_id"], True)
        legacy = await store.list_generations({})
        first = legacy["items"][0]
        assert first["is_external"] and first["is_favorite"]
        assert first["allowed_actions"]["delete"] is False
        issues = []
        store.set_external_issue_handler(lambda *args: issues.append(args))
        reads = []
        original_read = Path.read_bytes

        def record_read(path):
            reads.append(path)
            return original_read(path)

        monkeypatch.setattr(Path, "read_bytes", record_read)
        assert await store.list_generations({"light": True}) == without_thumbnails(
            legacy
        )
        assert reads == issues == []

        # Listing indexed metadata must neither read missing external originals
        # nor alter their state. Actual image access still triggers reconciliation.
        original.unlink()
        assert await store.list_generations({"light": True}) == without_thumbnails(
            legacy
        )
        assert reads == issues == []
        await store.gallery_image_data(external["image_id"], detail="preview")
        assert ("nai", "missing") in issues
        await store.reconcile_external_source("nai", [])
        assert (await store.list_generations({"light": True}))["total"] == 0
        await store.close()

    asyncio.run(run())


def test_gallery_light_list_preserves_indexed_missing_local_state(tmp_path):
    async def run():
        app = await create_app(tmp_path, seed=False)
        store = app.state.plugin.store
        from astrbot_plugin_image_studio.tests.support.webui_harness import (
            fixture_image,
        )

        imported = await store.import_image(fixture_image(0), "local.png", {})
        with store._connect() as conn:
            conn.execute("UPDATE image_assets SET file_state='missing'")
        legacy = await store.list_generations({})
        light = await store.list_generations({"light": True})
        assert light == without_thumbnails(legacy)
        assert light["items"][0]["id"] == imported["generation_id"]
        assert light["items"][0]["file_state"] == "missing"
        await store.close()

    asyncio.run(run())
