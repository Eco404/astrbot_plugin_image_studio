from __future__ import annotations

import asyncio
import sqlite3
from dataclasses import replace
from pathlib import Path

import httpx
import pytest

from astrbot_plugin_image_studio.backend.gallery import store as storage
from astrbot_plugin_image_studio.backend.config import HistorySettings
from astrbot_plugin_image_studio.backend.models import ReferenceImage
from astrbot_plugin_image_studio.backend.metadata.exchange import export_parameters
from astrbot_plugin_image_studio.backend.gallery.store import (
    GenerationStore,
    ImportEditConflictError,
)
from astrbot_plugin_image_studio.tests.test_gallery_retention import picture, record
from astrbot_plugin_image_studio.tests.test_import_groups import (
    comfy_multi_output_image,
    image,
    stage,
)
from astrbot_plugin_image_studio.tests.webui_harness import create_app


def track_reads(monkeypatch, store):
    reads = []
    original = Path.read_bytes

    def read(path):
        if path.is_relative_to(store.assets_dir) or path.is_relative_to(
            store.thumbnails_dir
        ):
            reads.append(path)
        return original(path)

    monkeypatch.setattr(Path, "read_bytes", read)
    return reads


def test_single_image_context_preserves_exports_without_sibling_or_media_reads(
    tmp_path, monkeypatch
):
    async def run():
        store = GenerationStore(tmp_path)
        await store.initialize()
        group = await store.import_group(
            [
                stage(
                    store, image(seed=index), f"{index}.png", prompt=f"manual {index}"
                )
                for index in range(4)
            ],
            import_key="single-context",
        )
        generation_id = group["generation_id"]
        legacy = await store.generation_detail(generation_id, include_assets=False)
        selected = legacy["images"][2]
        expected = {
            name: export_parameters(legacy, selected["id"], name)
            for name in ("studio", "nai", "novelai")
        }
        reads = track_reads(monkeypatch, store)
        projections = []
        asset_item = store._asset_item

        def project(row, **kwargs):
            projections.append(row["id"])
            return asset_item(row, **kwargs)

        monkeypatch.setattr(store, "_asset_item", project)
        async with store._lock:
            context = await asyncio.wait_for(
                store.generation_image_context(generation_id, selected["id"]), 1
            )
        assert reads == [] and projections == [selected["id"]]
        assert len(context["images"]) == 1
        for name, exported in expected.items():
            assert export_parameters(context, selected["id"], name) == exported
        first = await store.generation_image_context(generation_id)
        assert first["images"][0]["id"] == legacy["images"][0]["id"]
        assert await store.generation_image_context("f" * 32) is None
        with pytest.raises(ValueError, match="不属于"):
            await store.generation_image_context(generation_id, "f" * 32)
        with store._connect() as conn:
            conn.execute(
                "UPDATE generation_images SET supplemental_json = '{}' WHERE id = ?",
                (selected["id"],),
            )
        old_import = await store.generation_detail(generation_id, include_assets=False)
        old_context = await store.generation_image_context(
            generation_id, selected["id"]
        )
        assert export_parameters(old_context, selected["id"]) == export_parameters(
            old_import, selected["id"]
        )
        await store.close()

    asyncio.run(run())


def test_reproduction_reads_only_retained_reference_bytes(tmp_path, monkeypatch):
    async def run():
        app = await create_app(tmp_path, seed=False)
        store = app.state.plugin.store
        reference = ReferenceImage(
            id="ref", filename="ref.png", data=picture("green"), mime_type="image/png"
        )
        generation_id = await record(
            store, [picture("red"), picture("blue")], references=(reference,)
        )
        manifest = await store.generation_detail(generation_id, light=True)
        reads = track_reads(monkeypatch, store)

        async def no_group(*args, **kwargs):
            raise AssertionError("reproduction must not read all gallery images")

        monkeypatch.setattr(store, "generation_detail", no_group)
        context = await store.generation_image_context(
            generation_id, manifest["images"][1]["id"]
        )
        assert context["references"][0]["available"] and reads == []
        result = await app.state.plugin._service.reproduction_plan(
            generation_id, manifest["images"][1]["id"]
        )
        assert result["reference_available"] and len(result["references"]) == 1
        assert len(reads) == 1 and reads[0].is_relative_to(store.assets_dir)
        await store.close()

    asyncio.run(run())


def test_media_identity_and_optional_previews_are_consistent(tmp_path, monkeypatch):
    async def run():
        store = GenerationStore(tmp_path)
        await store.initialize()
        imported = await store.import_image(image(), "one.png", {})
        generation_id = imported["generation_id"]
        cover = (await store.list_generations({}))["items"][0]
        reads = track_reads(monkeypatch, store)
        manifest = await store.generation_detail(generation_id, light=True)
        image_id = manifest["images"][0]["id"]
        sequence = (await store.gallery_image_sequence({}))[0]
        edit_manifest = await store.import_edit_snapshot(generation_id, light=True)
        item = edit_manifest["items"][0]
        hydrated = (
            await store.import_edit_snapshot(
                generation_id,
                image_id=image_id,
                item_revision=item["item_revision"],
                include_preview=False,
            )
        )["items"][0]
        info = await store.gallery_image_info(image_id, include_preview=False)
        assert (
            reads == []
            and not info["image"]["thumbnail_data_url"]
            and not hydrated["thumbnail_data_url"]
        )
        preview = await store.gallery_image_data(image_id, detail="preview")
        assert len(reads) == 1
        identities = {
            (entry["sha256"], entry["thumbnail_revision"])
            for entry in (
                cover,
                manifest["images"][0],
                sequence,
                item,
                hydrated,
                info["image"],
                preview,
            )
        }
        assert len(identities) == 1
        default = await store.gallery_image_info(image_id)
        assert (
            len(reads) == 2
            and default["image"]["thumbnail_data_url"] == preview["data_url"]
        )
        with store._connect() as conn:
            conn.execute(
                "UPDATE image_thumbnails SET quality = quality + 1 WHERE asset_id = ?",
                (cover["sha256"],),
            )
        changed = (await store.gallery_image_sequence({}))[0]
        assert changed["sha256"] == cover["sha256"]
        assert changed["thumbnail_revision"] != cover["thumbnail_revision"]
        await store.close()

    asyncio.run(run())


@pytest.mark.parametrize("wal", [False, True])
def test_gallery_revision_tracks_commits_not_reads_or_rollbacks(tmp_path, wal):
    async def run():
        store = GenerationStore(tmp_path)
        await store.initialize()
        if wal:
            with store._connect() as conn:
                conn.execute("PRAGMA journal_mode=WAL")
        first = await store.gallery_revision()
        assert set(
            await asyncio.gather(*(store.gallery_revision() for _ in range(8)))
        ) == {first}
        await store.import_image(image(), "one.png", {})
        second = await store.gallery_revision()
        assert (
            second != first and (await store.list_generations({}))["revision"] == second
        )
        with sqlite3.connect(store.db_path) as conn:
            conn.execute("UPDATE generations SET is_favorite = 1")
        third = await store.gallery_revision()
        assert third != second
        with sqlite3.connect(store.db_path) as conn:
            conn.execute("UPDATE generations SET is_favorite = 0")
            conn.rollback()
        assert await store.gallery_revision() == third
        await store.close()
        assert store._revision_connection is None
        assert await store.gallery_revision() != third
        await store.close()

    asyncio.run(run())


def test_gallery_retention_cache_invalidates_and_never_controls_cleanup(
    tmp_path, monkeypatch
):
    async def run():
        store = GenerationStore(tmp_path)
        await store.initialize()
        first = await record(store, [picture("red")])
        second = await record(store, [picture("blue")])
        clock = [1000.0]
        monkeypatch.setattr(storage.time, "time", lambda: clock[0])
        with store._connect() as conn:
            conn.execute(
                "UPDATE generations SET cleanup_protected_until = ? WHERE id = ?",
                (1001.0, first),
            )
        history = HistorySettings(True, 1, 0, False)
        calls = []
        calculate = store._retention_status_sync

        def counted(settings):
            calls.append(settings)
            return calculate(settings)

        monkeypatch.setattr(store, "_retention_status_sync", counted)
        results = await asyncio.gather(
            *(store.gallery_retention_status(history) for _ in range(6))
        )
        assert len(calls) == 1 and all(
            result["protected_records"] == 1 for result in results
        )
        results[0]["candidate_ids"].append("not-a-real-record")
        async with store._lock:
            hit = await asyncio.wait_for(store.gallery_retention_status(history), 1)
        assert hit["candidate_ids"] == [second] and len(calls) == 1
        await store.set_favorite(second, True)
        assert (await store.gallery_retention_status(history))["favorite_records"] == 1
        assert len(calls) == 2
        changed_settings = replace(history, max_records=100)
        await store.gallery_retention_status(changed_settings)
        assert len(calls) == 3
        clock[0] = 1001.1
        assert (await store.gallery_retention_status(changed_settings))[
            "protected_records"
        ] == 0
        assert len(calls) == 4
        clock[0] += 4
        await store.gallery_retention_status(changed_settings)
        assert len(calls) == 5
        with sqlite3.connect(store.db_path) as conn:
            conn.execute("UPDATE generations SET is_favorite = 0")
        await store.gallery_retention_status(history)
        assert len(calls) == 6
        await store.retention_status(history)
        assert len(calls) == 7
        await asyncio.to_thread(store._cleanup_sync, history)
        assert (await store.gallery_retention_status(history))["record_count"] == 1
        await store.close()

    asyncio.run(run())


def test_stored_editor_projection_is_revision_checked_and_omits_raw(
    tmp_path, monkeypatch
):
    async def run():
        app = await create_app(tmp_path, seed=False)
        store = app.state.plugin.store
        data, _ = comfy_multi_output_image()
        imported = await store.import_image(
            data, "comfy.png", {"comfy_output_node": "6", "prompt": "manual"}
        )
        generation_id = imported["generation_id"]
        manifest = await store.import_edit_snapshot(generation_id, light=True)
        item = manifest["items"][0]
        reads = track_reads(monkeypatch, store)
        projected = await store.project_import_edit_image(
            generation_id, item["image_id"], item["item_revision"], "16"
        )
        assert projected["normalized"]["prompt"] == "second branch prompt"
        assert "raw" not in projected and reads == []
        endpoint = f"/astrbot_plugin_image_studio/gallery/import-edit/{generation_id}"
        query = {"image_id": item["image_id"], "item_revision": item["item_revision"]}
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.get(
                endpoint, params={**query, "output_node_id": "16"}
            )
            assert response.status_code == 200 and response.json() == projected
            response = await client.get(
                endpoint, params={**query, "include_preview": 0}
            )
            assert (
                response.status_code == 200
                and not response.json()["items"][0]["thumbnail_data_url"]
            )
            response = await client.get(
                f"/astrbot_plugin_image_studio/gallery/image-info/{item['image_id']}",
                params={"include_preview": 0},
            )
            assert (
                response.status_code == 200
                and not response.json()["image"]["thumbnail_data_url"]
            )
            response = await client.get(
                f"/astrbot_plugin_image_studio/gallery/parameters/{generation_id}",
                params={"image_id": item["image_id"]},
            )
            assert response.status_code == 200 and reads == []
        untouched = await store.import_edit_snapshot(
            generation_id,
            image_id=item["image_id"],
            item_revision=item["item_revision"],
            include_preview=False,
        )
        assert untouched["items"][0]["fields"]["prompt"] == "manual"
        with pytest.raises(ValueError):
            await store.project_import_edit_image(
                generation_id, item["image_id"], item["item_revision"], "missing"
            )
        with pytest.raises(LookupError):
            await store.project_import_edit_image(
                "f" * 32, item["image_id"], item["item_revision"], "16"
            )
        with store._connect() as conn:
            conn.execute(
                "UPDATE image_metadata SET metadata_json = json_set(metadata_json, '$.repair', 1) WHERE asset_id = ?",
                (item["sha256"],),
            )
        with pytest.raises(ImportEditConflictError):
            await store.project_import_edit_image(
                generation_id, item["image_id"], item["item_revision"], "16"
            )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.get(
                endpoint, params={**query, "output_node_id": "16"}
            )
            assert response.status_code == 409
        await store.close()

    asyncio.run(run())
