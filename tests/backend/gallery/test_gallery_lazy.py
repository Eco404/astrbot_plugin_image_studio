from __future__ import annotations

import asyncio
import copy
from collections import OrderedDict
from pathlib import Path

import httpx
import pytest
from astrbot_plugin_image_studio.backend.gallery import projection as storage
from astrbot_plugin_image_studio.backend.gallery.errors import ImportEditConflictError
from astrbot_plugin_image_studio.backend.gallery.store import GenerationStore
from astrbot_plugin_image_studio.backend.metadata import parser as image_metadata
from astrbot_plugin_image_studio.tests.backend.gallery.test_gallery_api import PREFIX
from astrbot_plugin_image_studio.tests.support.gallery_images import (
    comfy_multi_output_image,
    image,
    stage,
)
from astrbot_plugin_image_studio.tests.support.webui_harness import create_app


async def large_group(store, count=100):
    entries = [
        stage(
            store,
            image(
                seed=index, prompt=f"image {index}: " + "workflow description " * 400
            ),
            f"image-{index}.png",
            model="same",
            mode="text2img",
        )
        for index in range(count)
    ]
    return (await store.import_group(entries, import_key="lazy-group"))["generation_id"]


def track_image_reads(monkeypatch, store):
    calls = []
    original = Path.read_bytes

    def read_bytes(path):
        if path.is_relative_to(store.assets_dir) or path.is_relative_to(
            store.thumbnails_dir
        ):
            calls.append(path)
        return original(path)

    monkeypatch.setattr(Path, "read_bytes", read_bytes)
    return calls


def test_large_group_manifests_and_single_image_api_are_lazy(tmp_path, monkeypatch):
    async def run():
        app = await create_app(tmp_path, seed=False)
        store = app.state.plugin.store
        generation_id = await large_group(store)
        reads = track_image_reads(monkeypatch, store)
        projected = []
        original_asset = store.queries.asset_item

        def asset_item(row, **kwargs):
            projected.append(row["id"])
            return original_asset(row, **kwargs)

        monkeypatch.setattr(store.queries, "asset_item", asset_item)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.get(
                PREFIX + f"gallery/detail/{generation_id}?light=1"
            )
            assert response.status_code == 200
            manifest = response.json()
            assert manifest["lightweight"] is True
            assert len(manifest["images"]) == 100
            assert "supplemental" not in manifest and "parameters" not in manifest
            assert "original_prompt" not in manifest
            assert all(
                not {"metadata", "supplemental", "data_url", "thumbnail_data_url"}
                & item.keys()
                for item in manifest["images"]
            )
            assert reads == projected == []
            first = manifest["images"][37]
            info = await client.get(PREFIX + "gallery/image-info/" + first["id"])
            assert info.status_code == 200
            assert info.json()["image"]["id"] == first["id"]
            assert info.json()["image"]["metadata"]["raw"]
            assert (
                info.json()["image"]["download_filename"] == first["download_filename"]
            )
            assert "images" not in info.json()["detail_fields"]
            assert len(reads) == 1 and projected == [first["id"]]
            for quality in ("preview", "original"):
                reads.clear()
                data = await client.get(
                    PREFIX + "gallery/image/" + first["id"], params={"detail": quality}
                )
                assert data.status_code == 200 and data.json()["image_index"] == 37
                assert data.json()["data_url"].startswith("data:image/")
                assert len(reads) == 1
                assert projected == [first["id"]]
            reads.clear()
            download = await store.gallery_image_file(first["id"])
            assert download[2] == first["download_filename"]
            assert reads == [] and projected == [first["id"]]
            legacy = await client.get(
                PREFIX + f"gallery/detail/{generation_id}?assets=0"
            )
            assert legacy.status_code == 200
            assert len(reads) == 100
            assert all(item["thumbnail_data_url"] for item in legacy.json()["images"])
            assert len(response.content) < len(legacy.content) / 8
            assert [item["download_filename"] for item in legacy.json()["images"]] == [
                item["download_filename"] for item in manifest["images"]
            ]
            for path in (
                "gallery/image-info/invalid",
                "gallery/reference-image/invalid",
            ):
                assert (await client.get(PREFIX + path)).status_code == 404

    asyncio.run(run())


def test_editor_manifest_and_item_preserve_save_revision_without_global_lock(
    tmp_path, monkeypatch
):
    async def run():
        app = await create_app(tmp_path, seed=False)
        store = app.state.plugin.store
        generation_id = await large_group(store)
        reads = track_image_reads(monkeypatch, store)
        async with store._lock:
            manifest = await asyncio.wait_for(
                store.import_edit_snapshot(generation_id, light=True), 2
            )
        assert reads == []
        assert len(manifest["items"]) == 100
        first = manifest["items"][42]
        assert first["filename"] == "image-42.png"
        assert (
            not {"metadata", "fields", "parameters_json", "thumbnail_data_url"}
            & first.keys()
        )
        original_revision = store.imports.import_edit_revision

        def no_group_hash(*args):
            raise AssertionError("individual reads must not hash the group")

        monkeypatch.setattr(store.imports, "import_edit_revision", no_group_hash)
        async with store._lock:
            selected = await asyncio.wait_for(
                store.import_edit_snapshot(
                    generation_id,
                    image_id=first["image_id"],
                    item_revision=first["item_revision"],
                ),
                2,
            )
        assert len(reads) == 1
        assert len(selected["items"]) == 1
        assert selected["items"][0]["fields"]["prompt"].startswith("image 42: ")
        monkeypatch.setattr(store.imports, "import_edit_revision", original_revision)
        legacy = await store.import_edit_snapshot(generation_id)
        assert manifest["revision"] == legacy["revision"]
        assert {
            k: v for k, v in selected["items"][0].items() if k != "item_revision"
        } == legacy["items"][42]

        # A sibling's raw metadata changes do not rehash or invalidate the active
        # item, while the original complete-save revision still detects them.
        with store._connect() as conn:
            conn.execute(
                "UPDATE image_metadata SET metadata_json = json_set(metadata_json, '$.new', 1) WHERE asset_id = ?",
                (manifest["items"][0]["sha256"],),
            )
        await store.import_edit_snapshot(
            generation_id,
            image_id=first["image_id"],
            item_revision=first["item_revision"],
        )
        with pytest.raises(ImportEditConflictError):
            await store.edit_import(
                generation_id,
                manifest["revision"],
                [{"image_id": item["image_id"]} for item in manifest["items"]],
            )
        with store._connect() as conn:
            conn.execute(
                "UPDATE image_metadata SET metadata_json = json_set(metadata_json, '$.new', 1) WHERE asset_id = ?",
                (first["sha256"],),
            )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            endpoint = PREFIX + f"gallery/import-edit/{generation_id}"
            assert (await client.get(endpoint, params={"light": 1})).status_code == 200
            assert (
                await client.get(endpoint, params={"image_id": first["image_id"]})
            ).status_code == 400
            assert (
                await client.get(
                    endpoint,
                    params={
                        "image_id": first["image_id"],
                        "item_revision": first["item_revision"],
                    },
                )
            ).status_code == 409
            assert (
                await client.get(
                    endpoint,
                    params={
                        "image_id": "f" * 32,
                        "item_revision": first["item_revision"],
                    },
                )
            ).status_code == 404

    asyncio.run(run())


def test_comfy_projection_cache_is_bounded_and_invalidates_all_inputs(monkeypatch):
    monkeypatch.setattr(storage, "_COMFY_PROJECTION_CACHE", OrderedDict())
    monkeypatch.setattr(storage, "_COMFY_PROJECTION_CACHE_LIMIT", 2)
    _, raw = comfy_multi_output_image()
    metadata = image_metadata.parse_metadata_fields(raw, width=24, height=32)
    parser = image_metadata.parse_metadata_fields
    calls = []

    def counted(*args, **kwargs):
        calls.append(kwargs)
        return parser(*args, **kwargs)

    monkeypatch.setattr(image_metadata, "parse_metadata_fields", counted)
    first = storage.project_import_metadata(
        metadata, {"comfy_output_node": "6", "prompt": "manual one"}
    )
    first["normalized"]["prompt"] = "mutated response"
    second = storage.project_import_metadata(
        metadata, {"comfy_output_node": "6", "prompt": "manual two"}
    )
    assert second["normalized"]["prompt"] == "first branch prompt" and len(calls) == 1
    other_output = storage.project_import_metadata(
        metadata, {"comfy_output_node": "16"}
    )
    assert (
        other_output["normalized"]["prompt"] == "second branch prompt"
        and len(calls) == 2
    )
    changed = copy.deepcopy(metadata)
    changed["raw"]["extra"] = "raw changed"
    storage.project_import_metadata(changed, {"comfy_output_node": "6"})
    assert len(calls) == 3 and len(storage._COMFY_PROJECTION_CACHE) == 2
    storage.project_import_metadata(metadata, {"comfy_output_node": "6"})
    assert len(calls) == 4  # Oldest of three projections was evicted.
    monkeypatch.setattr(
        image_metadata, "PARSER_VERSION", image_metadata.PARSER_VERSION + 1
    )
    storage.project_import_metadata(metadata, {"comfy_output_node": "6"})
    assert len(calls) == 5
    changed = copy.deepcopy(metadata)
    changed["parser_version"] += 1
    storage.project_import_metadata(changed, {"comfy_output_node": "6"})
    assert len(calls) == 6
    changed["normalized"]["file_dimensions"]["width"] += 1
    storage.project_import_metadata(changed, {"comfy_output_node": "6"})
    assert len(calls) == 7
    storage._COMFY_PROJECTION_CACHE.clear()
    monkeypatch.setattr(storage, "_COMFY_PROJECTION_CACHE_BYTES", 64)
    storage.project_import_metadata(metadata, {"comfy_output_node": "6"})
    assert storage._COMFY_PROJECTION_CACHE == {}


def test_lazy_reference_reads_only_requested_reference(tmp_path, monkeypatch):
    async def run():
        store = GenerationStore(tmp_path)
        await store.initialize()
        generation_id = await large_group(store, count=2)
        manifest = await store.generation_detail(generation_id, light=True)
        with store._connect() as conn:
            for index, item in enumerate(manifest["images"]):
                conn.execute(
                    "INSERT INTO generation_references (id, generation_id, ordinal, filename, mime_type, size_bytes, asset_id) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        str(index) * 32,
                        generation_id,
                        index,
                        f"reference-{index}.png",
                        item["mime_type"],
                        item["size_bytes"],
                        item["sha256"],
                    ),
                )
        reads = track_image_reads(monkeypatch, store)
        manifest = await store.generation_detail(generation_id, light=True)
        assert len(manifest["references"]) == 2 and reads == []
        assert all("data_url" not in reference for reference in manifest["references"])
        selected = await store.gallery_reference_image("1" * 32)
        assert selected["available"] and selected["data_url"].startswith("data:image/")
        assert len(reads) == 1

    asyncio.run(run())
