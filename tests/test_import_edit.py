from __future__ import annotations

import asyncio
import json
import sqlite3
import zipfile

import httpx
import pytest
from astrbot_plugin_image_studio.backend.gallery.store import (
    GenerationStore,
    ImportEditConflictError,
)
from astrbot_plugin_image_studio.tests.test_gallery_api import PREFIX
from astrbot_plugin_image_studio.tests.test_import_groups import (
    comfy_multi_output_image,
    image,
    stage,
)
from astrbot_plugin_image_studio.tests.webui_harness import create_app


def database_state(store):
    with store._connect() as conn:
        return list(conn.iterdump())


def source_state(store):
    with store._connect() as conn:
        metadata = [tuple(row) for row in conn.execute("SELECT * FROM image_metadata")]
        receipts = [tuple(row) for row in conn.execute("SELECT * FROM import_batches")]
    assets = {
        str(path.relative_to(store.data_dir)): path.read_bytes()
        for directory in (store.assets_dir, store.thumbnails_dir, store.imports_dir)
        for path in directory.rglob("*")
        if path.is_file()
    }
    return metadata, receipts, assets


async def group_record(store):
    entries = [
        stage(store, image(), "first.png", model="same", mode="text2img"),
        stage(
            store, image("blue", seed=2), "second.png", model="same", mode="text2img"
        ),
    ]
    result = await store.import_group(entries, import_key="editable-group")
    return result["generation_id"], entries


def test_import_edit_roundtrip_reorder_preserves_originals_receipts_and_exports(
    tmp_path,
):
    async def run():
        store = GenerationStore(tmp_path)
        await store.initialize()
        generation_id, entries = await group_record(store)
        before = await store.generation_detail(generation_id, include_assets=False)
        with store._connect() as conn:
            for entry in before["images"]:
                previous = {
                    **entry["supplemental"],
                    "custom_extra": {"text": "原始信息"},
                }
                conn.execute(
                    "UPDATE generation_images SET supplemental_json=? WHERE id=?",
                    (json.dumps(previous, ensure_ascii=False), entry["id"]),
                )
        original_state = source_state(store)
        untouched = database_state(store)
        snapshot = await store.import_edit_snapshot(generation_id)
        assert await store.import_edit_snapshot(generation_id) == snapshot
        assert database_state(store) == untouched  # Opening and cancelling only reads.
        first, second = snapshot["items"]
        assert first["fields"]["model"] == "same"
        assert first["thumbnail_data_url"].startswith("data:image/")
        huge_seed = 9007199254740993123
        saved = await store.edit_import(
            generation_id,
            snapshot["revision"],
            [
                {
                    "image_id": second["image_id"],
                    "overrides": {
                        "model": "new-model",
                        "prompt": "",
                        "negative_prompt": "",
                        "generated_at": 1700000000,
                        "parameters": {"seed": huge_seed},
                    },
                },
                {
                    "image_id": first["image_id"],
                    "overrides": {
                        "prompt": "edited-search-keyword",
                        "generation_engine": "comfyui",
                        "mode": "img2img",
                    },
                },
            ],
        )
        assert saved["image_ids"] == [second["image_id"], first["image_id"]]
        assert saved["revision"] != snapshot["revision"]
        detail = await store.generation_detail(generation_id, include_assets=False)
        assert detail["model"] == "new-model" and detail["original_prompt"] == ""
        assert detail["mode"] == "unknown" and detail["generation_engine"] == "mixed"
        assert detail["generated_at"] == 1700000000
        assert detail["created_at"] == before["created_at"]
        for entry in detail["images"]:
            assert entry["supplemental"]["custom_extra"] == {"text": "原始信息"}
        assert (
            detail["supplemental"]["group_manifest"]
            == before["supplemental"]["group_manifest"]
        )
        assert source_state(store) == original_state
        edited = await store.import_edit_snapshot(generation_id)
        assert edited["items"][0]["fields"]["negative_prompt"] == ""
        assert edited["items"][0]["fields"]["parameters"] == {"seed": huge_seed}
        assert str(huge_seed) in edited["items"][0]["parameters_json"]
        assert (await store.list_generations({"query": "edited-search-keyword"}))[
            "items"
        ][0]["image_id"] == second["image_id"]
        assert [
            row["image_id"] for row in await store.gallery_image_sequence({})
        ] == saved["image_ids"]
        assert (await store.import_group(entries, import_key="editable-group"))[
            "duplicate"
        ]
        with zipfile.ZipFile(
            await store.export_generations([generation_id])
        ) as archive:
            exported = [
                json.loads(archive.read(name))
                for name in archive.namelist()
                if name.endswith(".json")
            ]
            assert [entry["image"]["id"] for entry in exported] == saved["image_ids"]
            assert exported[0]["model"] == "new-model"
            assert exported[0]["original_prompt"] == ""
            assert (
                exported[0]["image"]["supplemental"]["display_parameters"]["seed"]
                == huge_seed
            )
        restarted = GenerationStore(tmp_path)
        await restarted.initialize()
        assert (await restarted.import_edit_snapshot(generation_id))["items"] == edited[
            "items"
        ]

    asyncio.run(run())


def test_comfy_edit_projects_selected_branch_and_retains_manual_overrides(tmp_path):
    async def run():
        store = GenerationStore(tmp_path)
        await store.initialize()
        data, raw = comfy_multi_output_image()
        generation_id = (
            await store.import_image(
                data,
                "comfy.png",
                {
                    "comfy_output_node": "6",
                    "prompt": "manual text",
                    "negative_prompt": "",
                    "parameters": {"steps": 51},
                },
            )
        )["generation_id"]
        original_state = source_state(store)
        snapshot = await store.import_edit_snapshot(generation_id)
        entry = snapshot["items"][0]
        assert entry["output_node_id"] == "6"
        assert entry["metadata"]["normalized"]["prompt"] == "first branch prompt"
        assert entry["fields"]["prompt"] == "manual text"
        assert "prompt" in entry["edited_fields"]
        await store.edit_import(
            generation_id,
            snapshot["revision"],
            [
                {
                    "image_id": entry["image_id"],
                    "overrides": {"comfy_output_node": "16"},
                }
            ],
        )
        edited = (await store.import_edit_snapshot(generation_id))["items"][0]
        assert edited["output_node_id"] == "16"
        assert edited["metadata"]["normalized"]["prompt"] == "second branch prompt"
        assert edited["metadata"]["raw"] == raw
        assert edited["fields"]["prompt"] == "manual text"
        assert edited["fields"]["negative_prompt"] == ""
        assert edited["fields"]["parameters"] == {"steps": 51}
        detail = await store.generation_detail(generation_id, include_assets=False)
        assert detail["final_prompt"] == "second branch prompt"
        assert source_state(store) == original_state

    asyncio.run(run())


def test_legacy_supplemental_edit_retains_existing_values(tmp_path):
    async def run():
        store = GenerationStore(tmp_path)
        await store.initialize()
        generation_id = (
            await store.import_image(
                image(),
                "legacy.png",
                {
                    "prompt": "",
                    "negative_prompt": "manual negative",
                    "parameters": {"seed": 888},
                },
            )
        )["generation_id"]
        detail = await store.generation_detail(generation_id, include_assets=False)
        previous = detail["images"][0]["supplemental"]
        previous.pop("overrides")
        previous["extra"] = "legacy preserved"
        with store._connect() as conn:
            conn.execute(
                "UPDATE generation_images SET supplemental_json=?",
                (json.dumps(previous),),
            )
        snapshot = await store.import_edit_snapshot(generation_id)
        assert snapshot["items"][0]["fields"]["parameters"] == {"seed": 888}
        await store.edit_import(
            generation_id,
            snapshot["revision"],
            [
                {
                    "image_id": snapshot["items"][0]["image_id"],
                    "overrides": {"model": "updated"},
                }
            ],
        )
        current = (await store.generation_detail(generation_id, include_assets=False))[
            "images"
        ][0]["supplemental"]
        assert (
            current["prompt"] == "" and current["negative_prompt"] == "manual negative"
        )
        assert current["display_parameters"]["seed"] == 888
        assert current["extra"] == "legacy preserved"

    asyncio.run(run())


@pytest.mark.parametrize("change", ["edit", "append", "delete"])
def test_stale_edits_cannot_replace_concurrent_changes(tmp_path, change):
    async def run():
        store = GenerationStore(tmp_path)
        await store.initialize()
        generation_id, _ = await group_record(store)
        snapshot = await store.import_edit_snapshot(generation_id)
        payload = [
            {"image_id": entry["image_id"], "overrides": {}}
            for entry in snapshot["items"]
        ]
        if change == "edit":
            modified = [
                {**payload[0], "overrides": {"prompt": "concurrent"}},
                payload[1],
            ]
            await store.edit_import(generation_id, snapshot["revision"], modified)
        elif change == "append":
            await store.append_import_group(
                [stage(store, image("green"), "third.png")],
                generation_id,
                import_key="concurrent-append",
                expected_engine="novelai",
            )
        else:
            await store.delete_images(generation_id, [payload[0]["image_id"]])
        before = database_state(store)
        with pytest.raises(ImportEditConflictError, match="重新打开"):
            await store.edit_import(generation_id, snapshot["revision"], payload)
        assert database_state(store) == before

    asyncio.run(run())


def test_import_edit_sql_failure_rolls_back_all_images_and_summary(tmp_path):
    async def run():
        store = GenerationStore(tmp_path)
        await store.initialize()
        generation_id, _ = await group_record(store)
        snapshot = await store.import_edit_snapshot(generation_id)
        with store._connect() as conn:
            conn.execute(
                "CREATE TRIGGER reject_second_edit BEFORE UPDATE ON generation_images "
                "WHEN NEW.ordinal = 1 BEGIN SELECT RAISE(ABORT, 'test failure'); END"
            )
        before = database_state(store)
        with pytest.raises(sqlite3.IntegrityError, match="test failure"):
            await store.edit_import(
                generation_id,
                snapshot["revision"],
                [
                    {"image_id": entry["image_id"], "overrides": {"prompt": "changed"}}
                    for entry in reversed(snapshot["items"])
                ],
            )
        assert database_state(store) == before

    asyncio.run(run())


def test_import_edit_api_roundtrip_and_error_boundaries(tmp_path):
    async def run():
        app = await create_app(tmp_path, seed=False)
        store = app.state.plugin.store
        generation_id, _ = await group_record(store)
        endpoint = PREFIX + f"gallery/import-edit/{generation_id}"
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            before = database_state(store)
            response = await client.get(endpoint)
            assert response.status_code == 200, response.text
            snapshot = response.json()
            assert database_state(store) == before
            payload = {
                "revision": snapshot["revision"],
                "items": [
                    {"image_id": entry["image_id"], "overrides": {}}
                    for entry in snapshot["items"]
                ],
            }
            invalid = [
                [],
                {},
                {**payload, "revision": "bad"},
                {**payload, "items": []},
                {**payload, "items": [payload["items"][0]]},
                {**payload, "items": [payload["items"][0]] * 2},
                {**payload, "items": [payload["items"][0], {"image_id": "0" * 32}]},
            ]
            for overrides in (
                {"model": []},
                {"model": "x" * 241},
                {"mode": "invalid"},
                {"generation_engine": 123},
                {"generation_engine": "x" * 81},
                {"prompt": None},
                {"negative_prompt": []},
                {"parameters": []},
                {"generated_at": True},
                {"generated_at": "bad"},
                {"generated_at": -1},
                {"comfy_output_node": "6"},
                {"raw": {}},
                {"prompt": "x" * (1024 * 1024 + 1)},
            ):
                invalid.append(
                    {
                        **payload,
                        "items": [
                            {
                                **payload["items"][0],
                                "overrides": {"prompt": "must rollback"},
                            },
                            {**payload["items"][1], "overrides": overrides},
                        ],
                    }
                )
            for body in invalid:
                response = await client.post(endpoint, json=body)
                assert response.status_code == 400, response.text
                assert database_state(store) == before
            noop = await client.post(endpoint, json=payload)
            assert (
                noop.status_code == 200
                and noop.json()["revision"] == snapshot["revision"]
            )
            assert database_state(store) == before
            response = await client.post(
                endpoint, json={**payload, "items": list(reversed(payload["items"]))}
            )
            assert response.status_code == 200, response.text
            assert response.json()["image_ids"] == [
                entry["image_id"] for entry in reversed(payload["items"])
            ]
            reordered = (await client.get(endpoint)).json()
            assert {
                entry["image_id"]: entry["fields"] for entry in reordered["items"]
            } == {entry["image_id"]: entry["fields"] for entry in snapshot["items"]}
            assert (await client.post(endpoint, json=payload)).status_code == 409
            missing = PREFIX + "gallery/import-edit/" + "0" * 32
            assert (await client.get(missing)).status_code == 404
            assert (await client.post(missing, json=payload)).status_code == 404
            with store._connect() as conn:
                conn.execute(
                    "UPDATE generations SET source='webui' WHERE id=?", (generation_id,)
                )
            assert (await client.get(endpoint)).status_code == 400
            assert (await client.post(endpoint, json=payload)).status_code == 400

    asyncio.run(run())
