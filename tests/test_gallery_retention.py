from __future__ import annotations

import asyncio
import io
import json
import sqlite3
import time
import zipfile

import pytest
from PIL import Image, PngImagePlugin

from astrbot_plugin_image_studio.config import HistorySettings
from astrbot_plugin_image_studio.database_schema import DATABASE_VERSION
from astrbot_plugin_image_studio.models import (
    GeneratedImage,
    GenerationRequest,
    ImageProvider,
    ReferenceImage,
)
from astrbot_plugin_image_studio.storage import GenerationStore


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
        assert generated_detail["generation_engine"] == "nai"
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
            raw, "new.png", {"prompt": "overwrite"}, import_key="upload-1"
        )
        assert duplicate == {
            "generation_id": imported["generation_id"],
            "duplicate": True,
        }
        with pytest.raises(ValueError, match="另一张图片"):
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
        ordinary = await record(store, [red])
        favorite = await record(store, [green])
        imported = await store.import_image(blue, "import.png", {})
        await store.set_favorite(favorite, True)
        status = await store.retention_status(HistorySettings(True, 1, 0, False))
        assert status["record_count"] == 1 and status["near_limit"]
        assert status["candidate_ids"] == [ordinary]
        assert status["favorite_records"] == status["imported_records"] == 1
        await store.import_image(red, "shared.png", {})
        assert (await store.retention_status(HistorySettings(True, 1, 0, False)))[
            "size_bytes"
        ] == 0
        store._cleanup_sync(HistorySettings(True, 0, 0, False))
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
        settings = HistorySettings(True, 0, 0, False)
        store._cleanup_sync(settings)
        assert await store.generation_detail(ordinary) is None
        assert await store.generation_detail(protected) is not None
        status = await store.retention_status(settings)
        assert (
            status["over_limit"]
            and status["protected_records"] == 1
            and status["candidate_ids"] == []
        )
        with store._connect() as conn:
            conn.execute("UPDATE generations SET cleanup_protected_until = 0")
        store._cleanup_sync(settings)
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


def test_database_development_versions_are_explicit_and_unknown_are_rejected(tmp_path):
    async def run():
        store = GenerationStore(tmp_path)
        await store.initialize()
        assert (await store.maintenance_report())["stats"][
            "database_version"
        ] == DATABASE_VERSION
        with sqlite3.connect(store.db_path) as conn:
            assert conn.execute("PRAGMA user_version").fetchone()[0] == 0
            assert conn.execute(
                "SELECT target_version, dev_revision FROM schema_meta"
            ).fetchone() == (1, 1)
            conn.execute("UPDATE schema_meta SET dev_revision = 99")
        with pytest.raises(RuntimeError, match="开发修订"):
            await GenerationStore(tmp_path).initialize()
        with sqlite3.connect(store.db_path) as conn:
            assert (
                conn.execute("SELECT dev_revision FROM schema_meta").fetchone()[0] == 99
            )
            conn.execute("PRAGMA user_version = 2")
        with pytest.raises(RuntimeError, match="正式版本"):
            await GenerationStore(tmp_path).initialize()

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
            retention_hours=24,
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
        store._cleanup_sync(HistorySettings(True, -1, 1, False))
        assert await store.generation_detail(ordinary) is None
        status = await store.retention_status(HistorySettings(True, -1, 1, False))
        assert status["over_limit"] and status["candidate_ids"] == []
        assert len(list(store.assets_dir.rglob("*.png"))) == 3

    asyncio.run(run())


def test_schema_transaction_rolls_back_before_version_stamp(tmp_path, monkeypatch):
    import astrbot_plugin_image_studio.storage as storage_module

    def fail_stamp(conn):
        raise RuntimeError("simulated migration failure")

    monkeypatch.setattr(storage_module, "finish_development_schema", fail_stamp)
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
    import astrbot_plugin_image_studio.image_metadata as metadata_module

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
    import astrbot_plugin_image_studio.image_metadata as metadata_module

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

        def upgraded(data):
            result = parse(data)
            result["normalized"]["new_field"] = "upgraded-search"
            return result

        monkeypatch.setattr(metadata_module, "PARSER_VERSION", 2)
        monkeypatch.setattr(metadata_module, "parse_image_metadata", upgraded)
        await store.run_maintenance(
            HistorySettings(False, 0, 0, False),
            preview_max_edge=768,
            preview_quality=80,
        )
        after = await store.generation_detail(
            imported["generation_id"], include_assets=False
        )
        assert after["images"][0]["metadata"]["parser_version"] == 2
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

        monkeypatch.setattr(metadata_module, "PARSER_VERSION", 3)
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
