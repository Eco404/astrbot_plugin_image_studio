from __future__ import annotations

import asyncio
import hashlib
import sqlite3
import time

import pytest

from astrbot_plugin_image_studio.config import HistorySettings
from astrbot_plugin_image_studio.models import (
    GeneratedImage,
    GenerationRequest,
    ImageProvider,
    ReferenceImage,
)
from astrbot_plugin_image_studio.storage import GenerationStore, ImportDuplicateError
from astrbot_plugin_image_studio.tests.test_import_groups import image, stage


def declared(store, data, filename, **overrides):
    return {
        **stage(store, data, filename, **overrides),
        "sha256": hashlib.sha256(data).hexdigest(),
    }


async def generated(store, data, source="webui", references=()):
    return await store.record_success(
        provider=ImageProvider.from_mapping(
            {"id": "test", "kind": "nai_direct", "model": "original-model"}
        ),
        request=GenerationRequest(
            mode="text2img",
            source=source,
            provider_id="test",
            prompt="original prompt",
            references=references,
        ),
        images=(GeneratedImage(data, "image/png"),),
        elapsed_ms=1,
        history=HistorySettings(True, 0, 0, True),
    )


@pytest.mark.parametrize("source", ["webui", "command", "llm_tool", "import"])
def test_gallery_hash_membership_blocks_every_source_without_changing_old_record(
    tmp_path, source
):
    async def run():
        store = GenerationStore(tmp_path)
        await store.initialize()
        data = image()
        existing = (
            (
                await store.import_image(
                    data, "original.png", {"prompt": "original prompt"}
                )
            )["generation_id"]
            if source == "import"
            else await generated(store, data, source)
        )
        before = await store.generation_detail(existing, include_assets=False)
        digest = hashlib.sha256(data).hexdigest()
        result = await store.check_import_hashes([digest])
        assert result["allowed"] is False and result["code"] == "gallery_duplicates"
        assert result["duplicate_hashes"] == [digest]
        assert (
            "1 张画廊已有图片" in result["message"]
            and "整批上传已取消" in result["message"]
        )
        with pytest.raises(ImportDuplicateError):
            await store.import_image(
                data, "new-name.png", {"prompt": "changed", "model": "different-model"}
            )
        assert await store.generation_detail(existing, include_assets=False) == before
        assert len(list(store.assets_dir.rglob("*.png"))) == 1

    asyncio.run(run())


def test_reference_and_lease_only_assets_can_be_imported_without_copying_originals(
    tmp_path,
):
    async def run():
        store = GenerationStore(tmp_path)
        await store.initialize()
        reference_data, lease_data = image(seed=2), image(seed=3)
        await generated(
            store,
            image(seed=1),
            references=(
                ReferenceImage("ref", "reference.png", reference_data, "image/png"),
            ),
        )
        await store.lease_agent_images(
            (GeneratedImage(lease_data, "image/png"),),
            scope_id="session",
            create_preview=True,
            preview_max_edge=768,
            preview_quality=80,
        )
        entries = [
            declared(store, reference_data, "reference.png"),
            declared(store, lease_data, "lease.png"),
        ]
        assert (await store.check_import_hashes([item["sha256"] for item in entries]))[
            "allowed"
        ]
        result = await store.commit_import_batch(
            entries, import_key="eligible", mode="separate"
        )
        assert len(result["generation_ids"]) == 2
        assert len(list(store.assets_dir.rglob("*.png"))) == 3
        assert len(list(store.thumbnails_dir.glob("*.webp"))) == 3
        with store._connect() as conn:
            assert conn.execute("SELECT COUNT(*) FROM image_assets").fetchone()[0] == 3

    asyncio.run(run())


@pytest.mark.parametrize("mode", ["separate", "group", "merge"])
def test_duplicates_inside_each_batch_mode_are_rejected_atomically(tmp_path, mode):
    async def run():
        store = GenerationStore(tmp_path)
        await store.initialize()
        target = (
            (await store.import_image(image(seed=1), "target.png", {}))["generation_id"]
            if mode == "merge"
            else ""
        )
        data = image(seed=2)
        entries = [declared(store, data, "one.png"), declared(store, data, "two.png")]
        checked = await store.check_import_hashes([item["sha256"] for item in entries])
        assert checked["code"] == "batch_duplicates"
        with pytest.raises(ImportDuplicateError) as failure:
            await store.commit_import_batch(
                entries,
                import_key="duplicated",
                mode=mode,
                target_id=target,
                expected_engine="novelai" if target else "",
            )
        assert failure.value.as_dict()["code"] == "batch_duplicates"
        assert (await store.list_generations({}))["total"] == (1 if target else 0)
        assert await store.get_import_batch_result("duplicated") is None

    asyncio.run(run())


@pytest.mark.parametrize("mode", ["separate", "group", "merge"])
def test_gallery_duplicate_cancels_every_new_member_of_batch(tmp_path, mode):
    async def run():
        store = GenerationStore(tmp_path)
        await store.initialize()
        existing_data = image(seed=1)
        target = (await store.import_image(existing_data, "target.png", {}))[
            "generation_id"
        ]
        before = await store.generation_detail(target, include_assets=False)
        entries = [
            declared(store, image(seed=2), "new.png"),
            declared(store, existing_data, "existing.png"),
        ]
        with pytest.raises(ImportDuplicateError):
            await store.commit_import_batch(
                entries,
                import_key="blocked",
                mode=mode,
                target_id=target if mode == "merge" else "",
                expected_engine="novelai" if mode == "merge" else "",
            )
        assert await store.generation_detail(target, include_assets=False) == before
        assert len(list(store.assets_dir.rglob("*.png"))) == 1
        assert len(list(store.thumbnails_dir.glob("*.webp"))) == 1

    asyncio.run(run())


def test_actual_hash_must_match_declared_hash_before_any_gallery_mutation(tmp_path):
    async def run():
        store = GenerationStore(tmp_path)
        await store.initialize()
        entry = declared(store, image(seed=1), "actual.png")
        entry["sha256"] = hashlib.sha256(image(seed=2)).hexdigest()
        with pytest.raises(ValueError, match="实际图片 SHA-256 与声明不一致"):
            await store.commit_import_batch([entry], import_key="forged")
        assert (await store.list_generations({}))["total"] == 0
        assert not list(store.assets_dir.rglob("*.png"))
        assert await store.get_import_batch_result("forged") is None

    asyncio.run(run())


def test_two_store_instances_racing_same_image_allow_exactly_one_import(tmp_path):
    async def run():
        first, second = GenerationStore(tmp_path), GenerationStore(tmp_path)
        await first.initialize()
        await second.initialize()
        entries = [declared(first, image(), "shared.png")]
        results = await asyncio.gather(
            first.commit_import_batch(entries, import_key="one"),
            second.commit_import_batch(entries, import_key="two"),
            return_exceptions=True,
        )
        assert sum(isinstance(result, dict) for result in results) == 1
        assert sum(isinstance(result, ImportDuplicateError) for result in results) == 1
        assert (await first.list_generations({}))["total"] == 1
        assert len(list(first.assets_dir.rglob("*.png"))) == 1
        assert next(first.assets_dir.rglob("*.png")).read_bytes() == image()

    asyncio.run(run())


def test_separate_batch_failure_on_second_record_rolls_back_first_and_receipt(
    tmp_path, monkeypatch
):
    async def run():
        store = GenerationStore(tmp_path)
        await store.initialize()
        entries = [declared(store, image(seed=i), f"{i}.png") for i in (1, 2, 3)]
        original = store._refresh_search_sync
        calls = 0

        def fail_second(conn, generation_id):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise sqlite3.IntegrityError("second record failed")
            original(conn, generation_id)

        monkeypatch.setattr(store, "_refresh_search_sync", fail_second)
        with pytest.raises(sqlite3.IntegrityError, match="second record failed"):
            await store.commit_import_batch(entries, import_key="atomic-separate")
        assert calls == 2
        assert (await store.list_generations({}))["total"] == 0
        assert not list(store.assets_dir.rglob("*.png"))
        assert not list(store.thumbnails_dir.glob("*.webp"))
        assert all(entry["path"].exists() for entry in entries)
        assert await store.get_import_batch_result("atomic-separate") is None
        with store._connect() as conn:
            assert conn.execute("SELECT COUNT(*) FROM image_assets").fetchone()[0] == 0
            assert (
                conn.execute("SELECT COUNT(*) FROM image_metadata").fetchone()[0] == 0
            )

    asyncio.run(run())


@pytest.mark.parametrize("mode", ["separate", "group", "merge"])
def test_import_receipts_survive_restart_and_deletion_without_recreating_records(
    tmp_path, mode
):
    async def run():
        store = GenerationStore(tmp_path)
        await store.initialize()
        target = (
            (await store.import_image(image(seed=1), "target.png", {}))["generation_id"]
            if mode == "merge"
            else ""
        )
        entries = [declared(store, image(seed=i), f"{i}.png") for i in (2, 3)]
        kwargs = {
            "import_key": "survives",
            "mode": mode,
            "target_id": target,
            "expected_engine": "novelai" if target else "",
        }
        result = await store.commit_import_batch(entries, **kwargs)
        assert result["added"] == 2
        assert (await store.commit_import_batch(entries, **kwargs))["duplicate"]
        for entry in entries:
            entry["path"].unlink()
        for generation_id in result["generation_ids"]:
            await store.delete_generation(generation_id)
        reopened = GenerationStore(tmp_path)
        await reopened.initialize()
        assert await reopened.get_import_batch_result("survives") == result
        replay = await reopened.commit_import_batch(entries, **kwargs)
        assert replay["duplicate"] and replay["added"] == replay["image_count"] == 0
        assert (await reopened.list_generations({}))["total"] == 0
        assert not list(reopened.assets_dir.rglob("*.png"))

    asyncio.run(run())


def test_receipt_fingerprint_rejects_changed_parameters_before_returning_old_result(
    tmp_path,
):
    async def run():
        store = GenerationStore(tmp_path)
        await store.initialize()
        entries = [declared(store, image(), "original.png", prompt="confirmed")]
        result = await store.commit_import_batch(entries, import_key="same-key")
        entries[0]["overrides"]["prompt"] = "different"
        with pytest.raises(ValueError, match="顺序或参数"):
            await store.commit_import_batch(entries, import_key="same-key")
        assert (await store.generation_detail(result["generation_id"]))[
            "original_prompt"
        ] == "confirmed"

    asyncio.run(run())


def test_receipts_expire_after_task_window_without_deleting_gallery_records(tmp_path):
    async def run():
        store = GenerationStore(tmp_path)
        await store.initialize()
        result = await store.commit_import_batch(
            [declared(store, image(), "image.png")], import_key="expires"
        )
        with store._connect() as conn:
            expiry = conn.execute(
                "SELECT expires_at FROM import_batches WHERE id='expires'"
            ).fetchone()[0]
            assert expiry - time.time() > 23 * 3600
            conn.execute("UPDATE import_batches SET expires_at = 0")
        assert await store.get_import_batch_result("expires") is None
        report = await store.run_maintenance(
            HistorySettings(False, 0, 0, False),
            preview_max_edge=768,
            preview_quality=80,
        )
        assert report["repaired"]["expired_import_batches"] == 1
        assert await store.generation_detail(result["generation_id"]) is not None

    asyncio.run(run())


def test_prebaseline_receipt_layout_is_rejected_without_changing_gallery(tmp_path):
    async def run():
        store = GenerationStore(tmp_path)
        await store.initialize()
        result = await store.import_image(image(), "image.png", {"prompt": "kept"})
        with store._connect() as conn:
            for table in (
                "comfy_jobs",
                "comfy_workflow_revisions",
                "external_records",
                "external_sources",
            ):
                conn.execute(f"DROP TABLE {table}")
            conn.execute("DROP TABLE import_batches")
            conn.execute("DROP TABLE schema_meta")
            conn.execute(
                "CREATE TABLE schema_meta (id INTEGER PRIMARY KEY CHECK(id = 1), target_version INTEGER NOT NULL, dev_revision INTEGER NOT NULL)"
            )
            conn.execute("INSERT INTO schema_meta VALUES (1, 1, 2)")
            conn.execute("PRAGMA user_version = 0")
        with store._connect() as conn:
            before = list(conn.iterdump())
        reopened = GenerationStore(tmp_path)
        with pytest.raises(RuntimeError, match="末版开发版"):
            await reopened.initialize()
        with reopened._connect() as conn:
            assert list(conn.iterdump()) == before
            assert (
                conn.execute(
                    "SELECT original_prompt FROM generations WHERE id=?",
                    (result["generation_id"],),
                ).fetchone()[0]
                == "kept"
            )
            assert (
                conn.execute("SELECT dev_revision FROM schema_meta").fetchone()[0] == 2
            )
            assert conn.execute("PRAGMA user_version").fetchone()[0] == 0
            assert (
                conn.execute(
                    "SELECT name FROM sqlite_master WHERE name = 'import_batches'"
                ).fetchone()
                is None
            )

    asyncio.run(run())
