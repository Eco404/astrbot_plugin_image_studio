from __future__ import annotations

import asyncio
import io
import json
import sqlite3
import time
import zipfile

import pytest
from astrbot_plugin_image_studio.backend.config import HistorySettings
from astrbot_plugin_image_studio.backend.database.schema import DATABASE_VERSION
from astrbot_plugin_image_studio.backend.gallery.store import GenerationStore
from astrbot_plugin_image_studio.backend.models import (
    GeneratedImage,
    GenerationRequest,
    ImageProvider,
    ReferenceImage,
)
from PIL import Image, PngImagePlugin


def picture(color: str = "red", metadata: dict | None = None) -> bytes:
    result = io.BytesIO()
    info = PngImagePlugin.PngInfo()
    for key, value in (metadata or {}).items():
        info.add_text(key, value if isinstance(value, str) else json.dumps(value))
    Image.new("RGB", (48, 64), color).save(result, "PNG", pnginfo=info)
    return result.getvalue()


async def record(store: GenerationStore, images: list[bytes], *, references=()) -> str:
    return await store.record_success(
        provider=ImageProvider.from_mapping(
            {
                "id": "test",
                "name": "Test",
                "kind": "nai_direct",
                "model": "nai-diffusion-4-5-full",
            }
        ),
        request=GenerationRequest(
            mode="text2img",
            provider_id="test",
            prompt="user prompt",
            count=len(images),
            references=references,
        ),
        images=tuple(GeneratedImage(data, "image/png") for data in images),
        elapsed_ms=10,
        history=HistorySettings(True, -1, 0, True),
    )


def test_import_preserves_metadata_and_separates_request_snapshot(tmp_path):
    async def run():
        store = GenerationStore(tmp_path)
        await store.initialize()
        raw = picture(
            metadata={
                "Software": "NovelAI",
                "Comment": {
                    "prompt": "actual prompt",
                    "uc": "blurry",
                    "seed": 123,
                    "scale": 6,
                    "cfg_rescale": 0.3,
                },
            }
        )
        generated = await record(store, [raw])
        generated_detail = await store.generation_detail(
            generated, include_assets=False
        )
        assert generated_detail["original_prompt"] == "user prompt"
        assert generated_detail["final_prompt"] == "actual prompt"
        assert generated_detail["generation_engine"] == "novelai"
        assert generated_detail["provider_kind"] == "nai_direct"
        await store.delete_generation(generated)
        imported = await store.import_image(
            raw,
            "原图.png",
            {"prompt": "my correction", "parameters": {"extra": 0}},
            import_key="upload-1",
        )
        detail = await store.generation_detail(
            imported["generation_id"], include_assets=False
        )
        assert detail["source"] == "import"
        assert detail["provider_id"] == ""
        assert detail["parameters"] == {}
        assert detail["original_prompt"] == "my correction"
        assert detail["images"][0]["metadata"]["normalized"]["seed"] == 123
        assert detail["supplemental"]["display_parameters"]["extra"] == 0
        assert detail["supplemental"]["original_filename"] == "原图.png"
        duplicate = await store.import_image(
            raw,
            "原图.png",
            {"prompt": "my correction", "parameters": {"extra": 0}},
            import_key="upload-1",
        )
        assert duplicate == {
            "generation_id": imported["generation_id"],
            "duplicate": True,
        }
        with pytest.raises(ValueError, match="其他图片"):
            await store.import_image(
                picture("blue"), "other.png", {}, import_key="upload-1"
            )
        assert len(list(store.assets_dir.rglob("*.png"))) == 1
        assert (await store.list_generations({"query": "blurry", "source": "import"}))[
            "total"
        ] == 1
        archive = await store.export_generations([imported["generation_id"]])
        with zipfile.ZipFile(archive) as zipped:
            exported = json.loads(
                zipped.read(
                    next(name for name in zipped.namelist() if name.endswith(".json"))
                )
            )
            assert exported["image"]["metadata"]["normalized"]["seed"] == 123

    asyncio.run(run())


def test_quota_excludes_imports_favorites_and_shared_assets(tmp_path):
    async def run():
        store = GenerationStore(tmp_path)
        await store.initialize()
        red, green, blue = picture(), picture("green"), picture("blue")
        await store.import_image(red, "shared.png", {})
        ordinary = await record(store, [red])
        favorite = await record(store, [green])
        imported = await store.import_image(blue, "import.png", {})
        await store.set_favorite(favorite, True)
        status = await store.retention_status(HistorySettings(True, 1, 0, False))
        assert status["record_count"] == 1 and status["near_limit"]
        assert status["candidate_ids"] == [ordinary]
        assert status["favorite_records"] == 1 and status["imported_records"] == 2
        assert (await store.retention_status(HistorySettings(True, 1, 0, False)))[
            "size_bytes"
        ] == 0
        await record(store, [green])
        store.maintenance.cleanup(HistorySettings(True, 1, 0, False))
        assert await store.generation_detail(ordinary) is None
        assert await store.generation_detail(favorite) is not None
        assert await store.generation_detail(imported["generation_id"]) is not None
        assert len(list(store.assets_dir.rglob("*.png"))) == 3

    asyncio.run(run())


def test_unfavorite_grace_is_persistent_and_not_extended_by_retries(tmp_path):
    async def run():
        store = GenerationStore(tmp_path)
        await store.initialize()
        protected = await record(store, [picture()])
        ordinary = await record(store, [picture("blue")])
        await store.set_favorite(protected, True)
        result = await store.set_favorite(protected, False)
        assert 23 * 3600 < result["cleanup_protected_until"] - time.time() <= 24 * 3600
        assert (await store.set_favorite(protected, False))[
            "cleanup_protected_until"
        ] == result["cleanup_protected_until"]
        store = GenerationStore(tmp_path)
        await store.initialize()
        other_protected = await record(store, [picture("green")])
        await store.set_favorite(other_protected, True)
        await store.set_favorite(other_protected, False)
        settings = HistorySettings(True, 1, 0, False)
        store.maintenance.cleanup(settings)
        assert await store.generation_detail(ordinary) is None
        assert await store.generation_detail(protected) is not None
        status = await store.retention_status(settings)
        assert (
            status["over_limit"]
            and status["protected_records"] == 2
            and status["candidate_ids"] == []
        )
        with store._connect() as conn:
            conn.execute("UPDATE generations SET cleanup_protected_until = 0")
        store.maintenance.cleanup(settings)
        assert await store.generation_detail(protected) is None

    asyncio.run(run())


def test_partial_delete_keeps_cover_request_and_shared_reference(tmp_path):
    async def run():
        store = GenerationStore(tmp_path)
        await store.initialize()
        red, blue = picture(), picture("blue")
        generation = await record(
            store,
            [red, blue],
            references=(ReferenceImage("r", "reference.png", red, "image/png"),),
        )
        await store.set_favorite(generation, True)
        detail = await store.generation_detail(generation, include_assets=False)
        first, second = detail["images"]
        result = await store.delete_images(generation, [first["id"]])
        assert result == {
            "deleted": [first["id"]],
            "remaining": 1,
            "generation_deleted": False,
        }
        gallery = await store.list_generations({"favorite": "true"})
        assert gallery["items"][0]["image_id"] == second["id"]
        assert gallery["items"][0]["image_count"] == 1
        assert (await store.gallery_image_sequence({}))[0]["image_index"] == 0
        remaining = await store.generation_detail(generation, include_assets=False)
        assert remaining["parameters"]["count"] == 2
        assert remaining["is_favorite"] and remaining["references"][0]["available"]
        assert len(list(store.assets_dir.rglob("*.png"))) == 2
        with pytest.raises(ValueError, match="不属于"):
            await store.delete_images(generation, [second["id"], "0" * 32])
        assert len((await store.generation_detail(generation))["images"]) == 1
        result = await store.delete_images(generation, [second["id"]])
        assert result["generation_deleted"]
        assert not list(store.assets_dir.rglob("*.png"))
        assert not list(store.thumbnails_dir.rglob("*.webp"))

    asyncio.run(run())


def test_missing_original_keeps_import_record_and_metadata(tmp_path):
    async def run():
        store = GenerationStore(tmp_path)
        await store.initialize()
        imported = await store.import_image(picture(), "image.png", {})
        path = next(store.assets_dir.rglob("*.png"))
        path.unlink()
        report = await store.run_maintenance(
            HistorySettings(False, 0, 0, False),
            preview_max_edge=768,
            preview_quality=80,
        )
        assert report["status"] == "warning"
        detail = await store.generation_detail(
            imported["generation_id"], include_assets=False
        )
        assert detail is not None and detail["images"][0]["file_state"] == "unavailable"
        assert detail["images"][0]["metadata"]
        assert (await store.list_generations({}))["total"] == 1
        assert (await store.list_generations({}))["items"]

    asyncio.run(run())


def test_missing_thumbnail_rebuilt_for_protected_record(tmp_path):
    async def run():
        store = GenerationStore(tmp_path)
        await store.initialize()
        await store.import_image(picture(), "image.png", {})
        path = next(store.thumbnails_dir.glob("*.webp"))
        path.unlink()
        report = await store.run_maintenance(
            HistorySettings(False, 0, 0, False),
            preview_max_edge=768,
            preview_quality=80,
        )
        assert report["repaired"]["rebuilt_thumbnails"] == 1 and path.exists()

    asyncio.run(run())


def test_database_release_version_is_explicit_and_future_versions_are_rejected(
    tmp_path,
):
    async def run():
        store = GenerationStore(tmp_path)
        await store.initialize()
        assert (await store.maintenance_report())["stats"][
            "database_version"
        ] == DATABASE_VERSION
        with sqlite3.connect(store.db_path) as conn:
            assert conn.execute("PRAGMA user_version").fetchone()[0] == 4
            assert not conn.execute(
                "SELECT 1 FROM sqlite_master WHERE name='schema_meta'"
            ).fetchone()
            conn.execute("PRAGMA user_version = 5")
        with pytest.raises(RuntimeError, match="正式版本"):
            await GenerationStore(tmp_path).initialize()

    asyncio.run(run())


@pytest.mark.parametrize("limit", [0, -1])
def test_zero_record_limit_preserves_history_without_quota_warnings(tmp_path, limit):
    async def run():
        store = GenerationStore(tmp_path)
        await store.initialize()
        for color in ("red", "blue", "green"):
            await record(store, [picture(color)])
        settings = HistorySettings(True, limit, 0, False)
        saved = await store.record_success(
            provider=ImageProvider.from_mapping({"id": "test", "kind": "nai_direct"}),
            request=GenerationRequest(
                mode="text2img", provider_id="test", prompt="new result"
            ),
            images=(GeneratedImage(picture("yellow"), "image/png"),),
            elapsed_ms=1,
            history=settings,
        )
        assert saved
        store.maintenance.cleanup(settings)
        status = await store.retention_status(settings)
        assert status["record_count"] == 4
        assert status["limit_records"] == 0 and status["limit_bytes"] == 0
        assert not status["near_limit"] and not status["over_limit"]
        assert status["candidate_ids"] == []
        assert (await store.list_generations({}))["total"] == 4

    asyncio.run(run())


def test_quota_usage_splits_exempt_records_and_unique_assets(tmp_path):
    async def run():
        store = GenerationStore(tmp_path)
        await store.initialize()
        counted = await record(store, [picture("red")])
        imported = await store.import_image(picture("blue"), "import.png", {})
        await record(store, [picture("blue")])
        favorite = await record(store, [picture("green")])
        await store.set_favorite(favorite, True)
        await store.set_favorite(imported["generation_id"], True)
        await store.lease_agent_images(
            (GeneratedImage(picture("purple"), "image/png"),),
            scope_id="session",
            create_preview=True,
            preview_max_edge=768,
            preview_quality=80,
        )
        counted_asset = (await store.generation_detail(counted, include_assets=False))[
            "images"
        ][0]["sha256"]
        with store._connect() as conn:
            counted_bytes = conn.execute(
                "SELECT a.size_bytes + t.size_bytes FROM image_assets a JOIN image_thumbnails t ON t.asset_id = a.id WHERE a.id = ?",
                (counted_asset,),
            ).fetchone()[0]
        status = await store.retention_status(HistorySettings(True, 0, 0, False))
        assert status["record_count"] == 2
        assert status["exempt_record_count"] == 2
        assert status["imported_records"] == 1 and status["favorite_records"] == 2
        assert status["total_records"] == 4
        assert status["size_bytes"] == counted_bytes
        assert status["exempt_size_bytes"] > 0
        assert (
            status["gallery_size_bytes"]
            == status["size_bytes"] + status["exempt_size_bytes"]
        )
        assert status["total_size_bytes"] > status["gallery_size_bytes"]

    asyncio.run(run())


def test_batch_favorites_toggle_only_selected_and_unfavorite_grace(tmp_path):
    async def run():
        store = GenerationStore(tmp_path)
        await store.initialize()
        first = await record(store, [picture("red")])
        second = await record(store, [picture("blue")])
        unselected = await record(store, [picture("green")])
        imported = (await store.import_image(picture("yellow"), "import.png", {}))[
            "generation_id"
        ]
        await store.set_favorite(first, True)
        ids = [first, second, imported]
        state = await store.favorite_status(ids)
        assert state["action"] == "favorite" and not state["all_favorite"]
        assert state["favorite_count"] == 1 and state["selected_count"] == 3
        changed = await store.toggle_favorites(ids)
        assert changed["action"] == "favorite" and changed["all_favorite"]
        assert changed["changed_ids"] == [second, imported]
        assert (await store.generation_detail(unselected))["is_favorite"] is False
        assert (await store.favorite_status(ids))["action"] == "unfavorite"
        changed = await store.toggle_favorites(ids)
        assert changed["action"] == "unfavorite" and not changed["all_favorite"]
        assert changed["changed_ids"] == ids
        assert changed["items"][0]["cleanup_protected_until"] > time.time() + 23 * 3600
        assert (
            changed["items"][1]["cleanup_protected_until"]
            == changed["items"][0]["cleanup_protected_until"]
        )
        assert changed["items"][2]["cleanup_protected_until"] == 0

    asyncio.run(run())


def test_batch_favorites_rejects_missing_and_invalid_ids_without_partial_changes(
    tmp_path,
):
    async def run():
        store = GenerationStore(tmp_path)
        await store.initialize()
        identifier = await record(store, [picture()])
        missing = "0" * 32
        for method in (store.favorite_status, store.toggle_favorites):
            with pytest.raises(ValueError, match=missing):
                await method([identifier, missing])
            for invalid in ([], [identifier, "bad"], identifier, [identifier] * 1001):
                with pytest.raises(ValueError):
                    await method(invalid)
        assert not (await store.generation_detail(identifier))["is_favorite"]
        assert (await store.toggle_favorites([identifier, identifier]))[
            "changed_ids"
        ] == [identifier]

    asyncio.run(run())


def test_batch_favorite_transaction_failure_rolls_back_all_selected(tmp_path):
    async def run():
        store = GenerationStore(tmp_path)
        await store.initialize()
        first = await record(store, [picture()])
        second = await record(store, [picture("blue")])
        with store._connect() as conn:
            conn.execute(
                f"CREATE TRIGGER block_favorite BEFORE UPDATE OF is_favorite ON generations WHEN NEW.id = '{second}' BEGIN SELECT RAISE(ABORT, 'test update failure'); END"
            )
        with pytest.raises(sqlite3.IntegrityError, match="test update failure"):
            await store.toggle_favorites([first, second])
        status = await store.favorite_status([first, second])
        assert status["favorite_count"] == 0

    asyncio.run(run())


def test_warning_candidates_are_global_oldest_and_bounded(tmp_path):
    async def run():
        store = GenerationStore(tmp_path)
        await store.initialize()
        ids = [await record(store, [picture()]) for _ in range(12)]
        with store._connect() as conn:
            conn.executemany(
                "UPDATE generations SET created_at = ? WHERE id = ?",
                [(i, identifier) for i, identifier in enumerate(ids)],
            )
        assert not (await store.retention_status(HistorySettings(True, 20, 0, False)))[
            "near_limit"
        ]
        status = await store.retention_status(HistorySettings(True, 13, 0, False))
        assert status["near_limit"] and not status["over_limit"]
        assert status["candidate_ids"] == ids[:10]
        await store.set_favorite(ids[0], True)
        await store.set_favorite(ids[1], True)
        await store.set_favorite(ids[1], False)
        status = await store.retention_status(HistorySettings(True, 11, 0, False))
        assert status["candidate_ids"] == ids[2:]
        assert status["record_count"] == 11

    asyncio.run(run())


def test_capacity_cleanup_excludes_leases_and_protected_reference_shares(tmp_path):
    async def run():
        store = GenerationStore(tmp_path)
        await store.initialize()
        shared = picture("red")
        ordinary = await record(store, [shared])
        protected = await record(
            store,
            [picture("blue")],
            references=(ReferenceImage("r", "shared.png", shared, "image/png"),),
        )
        await store.set_favorite(protected, True)
        leased = await store.lease_agent_images(
            (GeneratedImage(picture("green"), "image/png"),),
            scope_id="scope",
            create_preview=True,
            preview_max_edge=768,
            preview_quality=80,
        )
        assert leased
        assert (await store.retention_status(HistorySettings(True, -1, 1, False)))[
            "size_bytes"
        ] == 0
        await store.set_favorite(protected, False)
        assert (await store.retention_status(HistorySettings(True, -1, 1, False)))[
            "size_bytes"
        ] > 0
        with store._connect() as conn:
            conn.execute("UPDATE image_assets SET size_bytes = 2 * 1024 * 1024")
        status = await store.retention_status(HistorySettings(True, -1, 1, False))
        assert status["over_limit"] and status["candidate_ids"] == [ordinary]
        store.maintenance.cleanup(HistorySettings(True, -1, 1, False))
        assert await store.generation_detail(ordinary) is None
        status = await store.retention_status(HistorySettings(True, -1, 1, False))
        assert status["over_limit"] and status["candidate_ids"] == []
        assert len(list(store.assets_dir.rglob("*.png"))) == 3

    asyncio.run(run())


def test_schema_transaction_rolls_back_before_version_stamp(tmp_path, monkeypatch):
    import astrbot_plugin_image_studio.backend.database.schema as schema_module

    def fail_stamp(conn):
        raise RuntimeError("simulated migration failure")

    monkeypatch.setattr(schema_module, "_stamp_release", fail_stamp)
    store = GenerationStore(tmp_path)
    with pytest.raises(RuntimeError, match="migration failure"):
        asyncio.run(store.initialize())
    with sqlite3.connect(store.db_path) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 0
        assert not conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()


def test_rejected_import_has_no_orphan_assets(tmp_path):
    async def run():
        store = GenerationStore(tmp_path)
        await store.initialize()
        with pytest.raises(ValueError, match="模式"):
            await store.import_image(picture(), "image.png", {"mode": "invalid"})
        with pytest.raises(ValueError, match="生成时间"):
            await store.import_image(picture(), "image.png", {"generated_at": "wrong"})
        assert not list(store.assets_dir.rglob("*.png"))
        assert not list(store.thumbnails_dir.rglob("*.webp"))
        assert (await store.list_generations({}))["total"] == 0

    asyncio.run(run())


@pytest.mark.parametrize("format_name", ["BMP", "TIFF"])
def test_import_rejects_unsupported_actual_format_even_with_png_filename(
    tmp_path, format_name
):
    async def run():
        store = GenerationStore(tmp_path)
        await store.initialize()
        output = io.BytesIO()
        Image.new("RGB", (8, 8), "red").save(output, format_name)
        with pytest.raises(ValueError, match="仅支持"):
            await store.import_image(output.getvalue(), "claimed.png", {})
        assert (await store.list_generations({}))["total"] == 0
        assert not list(store.assets_dir.rglob("*.*"))

    asyncio.run(run())


def test_import_pixel_limit_is_checked_even_when_metadata_is_cached(
    tmp_path, monkeypatch
):
    import astrbot_plugin_image_studio.backend.metadata.parser as metadata_module

    async def run():
        store = GenerationStore(tmp_path)
        await store.initialize()
        data = picture()
        generation = await record(store, [data])
        detail = await store.generation_detail(generation, include_assets=False)
        assert detail["images"][0]["metadata"]["parser_version"] > 0
        monkeypatch.setattr(metadata_module, "MAX_PIXELS", 100)
        with pytest.raises(ValueError, match="像素限制"):
            await store.import_image(data, "cached.png", {})
        assert (await store.list_generations({"source": "import"}))["total"] == 0
        assert await store.generation_detail(generation) is not None

    asyncio.run(run())


def test_metadata_parser_upgrade_refreshes_cache_without_losing_import(
    tmp_path, monkeypatch
):
    import astrbot_plugin_image_studio.backend.metadata.parser as metadata_module

    async def run():
        store = GenerationStore(tmp_path)
        await store.initialize()
        imported = await store.import_image(
            picture(
                metadata={
                    "Software": "NovelAI",
                    "Comment": {"prompt": "mountain", "uc": "blur", "seed": 123},
                }
            ),
            "image.png",
            {},
        )
        before = await store.generation_detail(
            imported["generation_id"], include_assets=False
        )
        parse = metadata_module.parse_image_metadata
        next_version = metadata_module.PARSER_VERSION + 1

        def upgraded(data):
            result = parse(data)
            result["normalized"]["prompt"] = "upgraded-search"
            return result

        monkeypatch.setattr(metadata_module, "PARSER_VERSION", next_version)
        monkeypatch.setattr(metadata_module, "parse_image_metadata", upgraded)
        await store.run_maintenance(
            HistorySettings(False, 0, 0, False),
            preview_max_edge=768,
            preview_quality=80,
        )
        after = await store.generation_detail(
            imported["generation_id"], include_assets=False
        )
        assert after["images"][0]["metadata"]["parser_version"] == next_version
        assert (
            after["images"][0]["metadata"]["raw"]
            == before["images"][0]["metadata"]["raw"]
        )
        assert (await store.list_generations({"query": "upgraded-search"}))[
            "total"
        ] == 1
        assert len(list(store.assets_dir.rglob("*.png"))) == 1

        def failed(data):
            raise ValueError("test metadata failure")

        monkeypatch.setattr(metadata_module, "PARSER_VERSION", next_version + 1)
        monkeypatch.setattr(metadata_module, "parse_image_metadata", failed)
        await store.run_maintenance(
            HistorySettings(False, 0, 0, False),
            preview_max_edge=768,
            preview_quality=80,
        )
        unchanged = await store.generation_detail(
            imported["generation_id"], include_assets=False
        )
        assert unchanged["images"][0]["metadata"] == after["images"][0]["metadata"]
        assert len(list(store.assets_dir.rglob("*.png"))) == 1

    asyncio.run(run())


def test_metadata_upgrade_and_import_projection_commit_together(tmp_path, monkeypatch):
    import astrbot_plugin_image_studio.backend.metadata.parser as metadata_module

    async def run():
        store = GenerationStore(tmp_path)
        await store.initialize()
        imported = await store.import_image(picture(), "image.png", {})
        before = await store.generation_detail(
            imported["generation_id"], include_assets=False
        )
        parse = metadata_module.parse_image_metadata
        version = metadata_module.PARSER_VERSION + 1

        def upgraded(data):
            result = parse(data)
            result["normalized"]["prompt"] = "new prompt"
            return result

        monkeypatch.setattr(metadata_module, "PARSER_VERSION", version)
        monkeypatch.setattr(metadata_module, "parse_image_metadata", upgraded)
        with store._connect() as conn:
            conn.execute(
                "CREATE TRIGGER block_projection BEFORE UPDATE OF original_prompt ON generations BEGIN SELECT RAISE(ABORT, 'projection failure'); END"
            )
        with pytest.raises(sqlite3.IntegrityError, match="projection failure"):
            store.metadata_records.backfill_metadata()
        after = await store.generation_detail(
            imported["generation_id"], include_assets=False
        )
        assert after == before
        with store._connect() as conn:
            conn.execute("DROP TRIGGER block_projection")
        store.metadata_records.backfill_metadata()
        after = await store.generation_detail(
            imported["generation_id"], include_assets=False
        )
        assert after["original_prompt"] == "new prompt"
        assert after["images"][0]["metadata"]["parser_version"] == version

    asyncio.run(run())


def test_metadata_upgrade_refreshes_import_projection_but_preserves_request_and_manual_values(
    tmp_path, monkeypatch
):
    import astrbot_plugin_image_studio.backend.metadata.parser as metadata_module

    async def run():
        original_version = metadata_module.PARSER_VERSION

        def old_parser(data):
            return {
                "format": "unknown",
                "parser_version": original_version,
                "raw": {"workflow": "original workflow"},
                "normalized": {"mode": "unknown"},
                "warnings": [],
            }

        monkeypatch.setattr(metadata_module, "parse_image_metadata", old_parser)
        store = GenerationStore(tmp_path)
        await store.initialize()
        generated = await record(store, [picture()])
        generated_before = await store.generation_detail(
            generated, include_assets=False
        )
        automatic = await store.import_image(picture("green"), "automatic.png", {})
        explicit = {
            "prompt": "",
            "negative_prompt": "",
            "model": "confirmed model",
            "mode": "unknown",
            "generation_engine": "unknown",
            "generated_at": None,
            "parameters": {"steps": 0, "sm": False, "custom": ""},
        }
        manual = await store.import_image(picture("blue"), "manual.png", explicit)

        def new_parser(data):
            return {
                "format": "comfyui",
                "parser_version": original_version + 1,
                "raw": {"workflow": "original workflow"},
                "normalized": {
                    "mode": "text2img",
                    "prompt": "newly resolved composition",
                    "negative_prompt": "new negative",
                    "model": "parsed model",
                    "steps": 30,
                    "sm": True,
                    "custom": "parsed value",
                    "generated_at": 1234,
                },
                "warnings": [],
            }

        monkeypatch.setattr(metadata_module, "PARSER_VERSION", original_version + 1)
        monkeypatch.setattr(metadata_module, "parse_image_metadata", new_parser)
        await GenerationStore(tmp_path).initialize()
        updated = await store.generation_detail(
            automatic["generation_id"], include_assets=False
        )
        assert updated["generation_engine"] == "comfyui"
        assert updated["model"] == "parsed model"
        assert updated["original_prompt"] == "newly resolved composition"
        assert updated["mode"] == "text2img"
        assert updated["generated_at"] == 1234
        assert updated["images"][0]["supplemental"]["display_parameters"]["steps"] == 30
        assert updated["images"][0]["supplemental"]["overrides"] == {}
        assert (
            await store.list_generations(
                {"source": "import", "generation_engine": "comfyui"}
            )
        )["total"] == 1
        corrected = await store.generation_detail(
            manual["generation_id"], include_assets=False
        )
        assert (
            corrected["original_prompt"] == ""
            and corrected["model"] == "confirmed model"
        )
        assert corrected["mode"] == corrected["generation_engine"] == "unknown"
        assert corrected["generated_at"] is None
        supplemental = corrected["images"][0]["supplemental"]
        assert supplemental["overrides"] == explicit
        assert supplemental["display_parameters"]["prompt"] == ""
        assert supplemental["display_parameters"]["negative_prompt"] == ""
        assert supplemental["display_parameters"]["steps"] == 0
        assert supplemental["display_parameters"]["sm"] is False
        assert supplemental["display_parameters"]["custom"] == ""
        assert supplemental["display_parameters"]["generated_at"] is None
        assert (
            corrected["images"][0]["metadata"]["normalized"]["prompt"]
            == "newly resolved composition"
        )
        assert (
            await store.list_generations(
                {"source": "import", "query": "newly resolved composition"}
            )
        )["total"] == 2
        generated_after = await store.generation_detail(generated, include_assets=False)
        for key in (
            "model",
            "mode",
            "provider_kind",
            "original_prompt",
            "final_prompt",
            "generation_engine",
            "parameters",
            "supplemental",
        ):
            assert generated_after[key] == generated_before[key]
        with store._connect() as conn:
            assert conn.execute("PRAGMA user_version").fetchone()[0] == 4

    asyncio.run(run())
