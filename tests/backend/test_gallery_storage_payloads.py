"""Gallery bodies are shared without changing detail/edit or lazy-list contracts."""

from __future__ import annotations

import asyncio
import json

import pytest

from astrbot_plugin_image_studio.backend.database.payloads import (
    PAYLOAD_KEY,
    gc_payloads,
)
from astrbot_plugin_image_studio.backend.gallery import storage
from astrbot_plugin_image_studio.backend.gallery.metadata_records import MetadataRecords
from astrbot_plugin_image_studio.backend.gallery.projection import _search_projection
from astrbot_plugin_image_studio.backend.gallery.store import GenerationStore
from astrbot_plugin_image_studio.backend.metadata.parser import parse_image_metadata
from astrbot_plugin_image_studio.tests.backend.test_import_groups import (
    comfy_multi_output_image,
    image,
    stage,
)


def test_compressed_metadata_and_derived_display_preserve_detail_and_editor(
    tmp_path, monkeypatch
):
    async def run():
        store = GenerationStore(tmp_path)
        await store.initialize()
        png, raw = comfy_multi_output_image()
        result = await store.import_group(
            [
                stage(
                    store,
                    png,
                    "first.png",
                    comfy_output_node="6",
                    model="shared-model",
                    prompt="manual",
                    parameters={"steps": 42},
                ),
                stage(
                    store,
                    comfy_multi_output_image("blue")[0],
                    "second.png",
                    comfy_output_node="16",
                    model="shared-model",
                ),
            ],
            import_key="body-sharing",
        )
        identity = result["generation_id"]
        detail = await store.generation_detail(identity, include_assets=False)
        first = detail["images"][0]
        assert first["metadata"]["raw"] == raw
        assert first["supplemental"]["display_parameters"]["steps"] == 42
        assert first["supplemental"]["display_parameters"]["prompt_candidates"]
        with store._connect() as conn:
            row = conn.execute(
                "SELECT supplemental_json FROM generations WHERE id=?", (identity,)
            ).fetchone()
            assert "display_parameters" not in json.loads(row[0])
            row = conn.execute(
                "SELECT supplemental_json FROM generation_images WHERE id=?",
                (first["id"],),
            ).fetchone()
            assert "prompt_candidates" not in json.loads(row[0])["display_parameters"]
            row = conn.execute(
                "SELECT metadata_json FROM image_metadata WHERE asset_id=?",
                (first["sha256"],),
            ).fetchone()
            compact = json.loads(row[0])
            assert PAYLOAD_KEY in compact["raw"]
            assert "prompt_candidates" not in compact["normalized"]
            assert storage.decode_metadata(conn, compact) == parse_image_metadata(png)
            assert not conn.execute(
                "SELECT 1 FROM sqlite_master WHERE name='generation_search'"
            ).fetchone()
        editor = await store.import_edit_snapshot(identity)
        assert editor["items"][0]["metadata"]["raw"] == raw
        assert editor["items"][0]["fields"]["prompt"] == "manual"
        assert editor["items"][0]["fields"]["parameters"]["steps"] == 42

        # Read-only navigation must never decompress graph/evidence payloads.
        def no_payload_reads(*args, **kwargs):
            raise AssertionError("lightweight navigation read a heavy payload")

        monkeypatch.setattr(storage, "load_payload", no_payload_reads)
        assert (
            await store.list_generations({"light": True, "sort": "latest_content"})
        )["total"] == 1
        assert len((await store.generation_detail(identity, light=True))["images"]) == 2
        assert (
            len((await store.import_edit_snapshot(identity, light=True))["items"]) == 2
        )
        assert len(await store.gallery_image_sequence({"sort": "latest_content"})) == 2

    asyncio.run(run())


def test_compact_snapshot_rebinds_owner_and_survives_original_deletion(tmp_path):
    async def run():
        store = GenerationStore(tmp_path)
        await store.initialize()
        result = await store.import_group(
            [
                stage(store, image("red"), "one.png"),
                stage(store, image("blue"), "two.png"),
            ],
            import_key="copied-payload",
        )
        identity = result["generation_id"]
        images = (await store.generation_detail(identity, light=True))["images"]
        graph = {"1": {"inputs": {"seed": 18446744073709551615}}}
        workflow = {"nodes": [{"id": 1, "widgets_values": [18446744073709551615]}]}
        snapshot = {
            "api_graph": graph,
            "api_graph_json": json.dumps(graph, indent=2),
            "workflow": workflow,
            "workflow_json": json.dumps(workflow),
        }
        with store._connect() as conn:
            first = storage.encode_supplemental(
                conn, "generation_images", images[0]["id"], {"comfyui": snapshot}
            )
            second = storage.encode_supplemental(
                conn, "generation_images", images[1]["id"], first
            )
            for item, encoded in zip(images, (first, second)):
                conn.execute(
                    "UPDATE generation_images SET supplemental_json=? WHERE id=?",
                    (json.dumps(encoded), item["id"]),
                )
            refs = conn.execute(
                "SELECT count(*) FROM storage_payload_refs WHERE slot='comfyui'"
            ).fetchone()[0]
            assert refs == 2
        await store.delete_images(identity, [images[0]["id"]])
        with store._connect() as conn:
            gc_payloads(conn)
            row = conn.execute(
                "SELECT supplemental_json FROM generation_images WHERE id=?",
                (images[1]["id"],),
            ).fetchone()
            assert storage.decode_supplemental(conn, row[0])["comfyui"] == snapshot
        await store.close()

    asyncio.run(run())


def test_rejected_metadata_update_keeps_body_ownership_and_user_marker_data(tmp_path):
    async def run():
        store = GenerationStore(tmp_path)
        await store.initialize()
        result = await store.import_image(image(), "metadata.png", {})
        detail = await store.generation_detail(
            result["generation_id"], include_assets=False
        )
        asset_id = detail["images"][0]["sha256"]
        original = detail["images"][0]["metadata"]
        with store._connect() as conn:
            MetadataRecords.save_metadata(
                conn,
                {
                    asset_id: {
                        **original,
                        "parser_version": original["parser_version"] - 1,
                        "raw": {"unexpected": "lower-version replacement"},
                    }
                },
            )
            gc_payloads(conn)
            row = conn.execute(
                "SELECT metadata_json FROM image_metadata WHERE asset_id=?", (asset_id,)
            ).fetchone()
            assert storage.decode_metadata(conn, row[0]) == original
            shaped = {
                "format": "unknown",
                "raw": {PAYLOAD_KEY: "f" * 64},
                "normalized": {"custom": {PAYLOAD_KEY: "e" * 64}},
            }
            encoded = storage.encode_metadata(conn, asset_id, shaped)
            assert storage.decode_metadata(conn, encoded) == shaped
        await store.close()

    asyncio.run(run())


def test_migration_retains_legacy_manual_display_and_is_repeatable(tmp_path):
    async def run():
        store = GenerationStore(tmp_path)
        await store.initialize()
        result = await store.import_image(image(), "legacy.png", {})
        identity = result["generation_id"]
        legacy = {
            "prompt": "manual historical text",
            "display_parameters": {
                "not_a_known_field": {"nested": "manually-entered"},
                "seed": 18446744073709551615,
            },
        }
        with store._connect() as conn:
            conn.execute(
                "UPDATE generation_images SET supplemental_json='{}' WHERE generation_id=?",
                (identity,),
            )
            conn.execute(
                "UPDATE generations SET supplemental_json=? WHERE id=?",
                (json.dumps(legacy), identity),
            )
            storage.migrate_gallery_storage(conn)
            storage.migrate_gallery_storage(conn)
            gc_payloads(conn)
            row = conn.execute(
                "SELECT supplemental_json FROM generations WHERE id=?", (identity,)
            ).fetchone()
            assert storage.decode_supplemental(conn, row[0]) == legacy
        detail = await store.generation_detail(identity, include_assets=False)
        assert detail["images"][0]["supplemental"] == legacy
        await store.close()

    asyncio.run(run())


def test_search_whitelist_excludes_graphs_ids_and_repeated_evidence():
    value = [
        {
            "prompt": "visible prompt",
            "seed": 123,
            "display_parameters": {
                "prompt": "visible prompt",
                "seed": 123,
                "prompt_candidates": [{"text": "unselected evidence"}],
                "api_graph_json": "HUGE_GRAPH",
                "workflow_json": "HUGE_WORKFLOW",
            },
            "parameters": {"sampler": "euler", "_fingerprint": "INTERNAL_ID"},
            "comfyui": {"api_graph": {"1": {"prompt": "LEAK"}}},
            "overrides": {"parameters": {"_private_field": "hidden"}},
        }
    ]
    result = _search_projection(value)
    assert result == "visible prompt 123 euler"


def test_search_keeps_custom_scalar_parameters_without_traversing_graphs():
    value = {
        "new_field": "unknown top level",
        "parameters": {
            "dress_color": "azure",
            "palette": ["cyan", "violet"],
            "tiling": True,
            "API_GRAPH_JSON": "execution graph",
            "workflow_json": "workflow graph",
            "nested_graph": {"prompt": "nested execution value"},
            "mixed_graph": ["not a scalar list", {"node": 1}],
            "_job_id": "internal task id",
            "fingerprint": "internal digest",
        },
        "overrides": {"parameters": {"dress_color": "azure", "custom_strength": 0.25}},
    }
    assert _search_projection(value) == "azure cyan violet True 0.25"


def test_imported_custom_parameters_are_searchable_and_restore_nested_values(
    tmp_path, monkeypatch
):
    from astrbot_plugin_image_studio.backend.metadata import parser

    async def run():
        original = parser.parse_image_metadata
        parameters = {
            "dress_color": "azure",
            "palette": ["cyan", "violet"],
            "nested": {"exact": {"value": "preserved"}},
            "workflow_json": "not searchable",
        }

        def parse(data):
            value = original(data)
            value["normalized"]["parameters"] = parameters
            return value

        monkeypatch.setattr(parser, "parse_image_metadata", parse)
        store = GenerationStore(tmp_path)
        await store.initialize()
        result = await store.import_image(image(), "custom.png", {})
        assert (await store.list_generations({"query": "azure"}))["total"] == 1
        assert (await store.list_generations({"query": "violet"}))["total"] == 1
        assert (await store.list_generations({"query": "not searchable"}))["total"] == 0
        detail = await store.generation_detail(
            result["generation_id"], include_assets=False
        )
        assert detail["images"][0]["metadata"]["normalized"]["parameters"] == parameters
        await store.close()

    asyncio.run(run())


def test_repeated_initialization_keeps_current_migration_backup_protected(tmp_path):
    async def run():
        store = GenerationStore(tmp_path)
        await store.initialize()
        protected = tmp_path / "backups" / "current-migration.sqlite3"
        store.maintenance.protected_backup = protected
        await store.initialize()
        assert store.maintenance.protected_backup == protected
        await store.close()

    asyncio.run(run())


@pytest.mark.parametrize(
    "table,column,value",
    [
        ("image_metadata", "metadata_json", "{broken"),
        ("generation_images", "supplemental_json", "[]"),
        ("generations", "supplemental_json", "null"),
        ("generations", "parameters_json", '"not an object"'),
    ],
)
def test_migration_rejects_corrupt_bodies_without_overwriting_existing_records(
    tmp_path, table, column, value
):
    async def run():
        store = GenerationStore(tmp_path)
        await store.initialize()
        await store.import_image(image(), "broken.png", {})
        with store._connect() as conn:
            conn.execute(f"UPDATE {table} SET {column}=?", (value,))
        with store._connect() as conn:
            before = list(conn.iterdump())
        with pytest.raises(ValueError, match="停止存储迁移"), store._connect() as conn:
            storage.migrate_gallery_storage(conn)
        with store._connect() as conn:
            assert list(conn.iterdump()) == before
        await store.close()

    asyncio.run(run())
