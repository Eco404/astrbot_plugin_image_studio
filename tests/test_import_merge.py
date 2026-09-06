from __future__ import annotations

import asyncio
import json
import sqlite3
import zipfile

import pytest

from astrbot_plugin_image_studio.config import HistorySettings
from astrbot_plugin_image_studio.models import (
    GeneratedImage,
    GenerationRequest,
    ImageProvider,
)
from astrbot_plugin_image_studio.storage import GenerationStore
from astrbot_plugin_image_studio.tests.test_import_groups import image, stage


def test_append_same_source_different_models_preserves_target_and_image_snapshots(
    tmp_path,
):
    async def run():
        store = GenerationStore(tmp_path)
        await store.initialize()
        original_data = image(model="old-model")
        target = await store.import_image(
            original_data,
            "original.png",
            {"generation_engine": "nai", "mode": "text2img"},
            import_key="original-import",
        )
        await store.set_favorite(target["generation_id"], True)
        before = await store.generation_detail(
            target["generation_id"], include_assets=False
        )
        other = await store.import_image(image("green"), "unrelated.png", {})
        unrelated_before = await store.generation_detail(
            other["generation_id"], include_assets=False
        )
        entries = [
            stage(
                store,
                image("blue", model="new-model", seed=222),
                "new.png",
                generation_engine="novelai",
                mode="img2img",
                prompt="new manual prompt",
            )
        ]
        merged = await store.append_import_group(
            entries,
            target["generation_id"],
            import_key="merge-1",
            expected_engine="nai",
        )
        assert merged == {
            "generation_id": target["generation_id"],
            "duplicate": False,
            "added": 1,
            "image_count": 2,
        }
        after = await store.generation_detail(
            target["generation_id"], include_assets=False
        )
        assert after["created_at"] == before["created_at"]
        assert after["is_favorite"] == before["is_favorite"] is True
        assert (
            after["model"] == "old-model"
            and after["original_prompt"] == before["original_prompt"]
        )
        assert after["mode"] == "unknown" and after["generation_engine"] == "novelai"
        assert after["images"][0]["id"] == before["images"][0]["id"]
        assert after["images"][0]["supplemental"] == before["images"][0]["supplemental"]
        assert after["images"][0]["metadata"] == before["images"][0]["metadata"]
        assert after["images"][1]["supplemental"]["prompt"] == "new manual prompt"
        assert after["images"][1]["metadata"]["normalized"]["seed"] == 222
        assert "new-model" in after["images"][1]["download_filename"]
        assert (await store.list_generations({"query": "new manual prompt"}))["items"][
            0
        ]["image_id"] == before["images"][0]["id"]
        assert (
            await store.generation_detail(other["generation_id"], include_assets=False)
            == unrelated_before
        )
        assert (
            await store.import_image(
                original_data,
                "original.png",
                {"generation_engine": "nai", "mode": "text2img"},
                import_key="original-import",
            )
        )["duplicate"]
        with zipfile.ZipFile(await store.export_generations([after["id"]])) as archive:
            assert after["images"][1]["download_filename"] in archive.namelist()
        with store._connect() as conn:
            assert (
                conn.execute(
                    "SELECT import_key FROM generations WHERE id = ?", (after["id"],)
                ).fetchone()[0]
                == "original-import"
            )
        assert (await store.retention_status(HistorySettings(True, 1, 1, False)))[
            "record_count"
        ] == 0

    asyncio.run(run())


def test_merge_target_listing_checks_every_image_source_and_filters(tmp_path):
    async def run():
        store = GenerationStore(tmp_path)
        await store.initialize()
        single = await store.import_image(
            image(),
            "single.png",
            {"prompt": "search target", "generation_engine": "nai"},
        )
        group = await store.import_group(
            [
                stage(
                    store, image("blue"), "a.png", model="same", generation_engine="nai"
                ),
                stage(
                    store,
                    image("green"),
                    "b.png",
                    model="same",
                    generation_engine="novelai",
                ),
            ],
            import_key="same-origin",
        )
        mixed = await store.import_group(
            [
                stage(
                    store,
                    image("black"),
                    "mixed-a.png",
                    model="same",
                    generation_engine="nai",
                ),
                stage(
                    store,
                    image("white"),
                    "mixed-b.png",
                    model="same",
                    generation_engine="comfyui",
                ),
            ],
            import_key="mixed-origin",
        )
        await store.import_image(
            image("yellow"), "unknown.png", {"generation_engine": "unknown"}
        )
        await store.record_success(
            provider=ImageProvider.from_mapping({"id": "test", "kind": "nai_direct"}),
            request=GenerationRequest(
                mode="text2img", provider_id="test", prompt="automatic"
            ),
            images=(GeneratedImage(image("purple"), "image/png"),),
            elapsed_ms=1,
            history=HistorySettings(True, 0, 0, False),
        )
        with store._connect() as conn:
            conn.execute(
                "UPDATE generations SET generation_engine='novelai' WHERE id = ?",
                (mixed["generation_id"],),
            )
        listed = await store.list_import_merge_targets("nai", limit=1)
        assert listed["total"] == 2 and len(listed["items"]) == 1
        next_page = await store.list_import_merge_targets("novelai", offset=1, limit=1)
        assert {listed["items"][0]["id"], next_page["items"][0]["id"]} == {
            single["generation_id"],
            group["generation_id"],
        }
        filtered = await store.list_import_merge_targets(
            "novelai", query="search target"
        )
        assert [row["id"] for row in filtered["items"]] == [single["generation_id"]]
        for invalid in ("", "unknown", "mixed", None):
            with pytest.raises(ValueError):
                await store.list_import_merge_targets(invalid)

    asyncio.run(run())


def test_append_rejects_unknown_mismatched_and_nonimport_targets_without_files(
    tmp_path,
):
    async def run():
        store = GenerationStore(tmp_path)
        await store.initialize()
        target = (await store.import_image(image(), "target.png", {}))["generation_id"]
        entry = stage(store, image("blue"), "new.png", generation_engine="unknown")
        with pytest.raises(ValueError, match="相同的已知来源"):
            await store.append_import_group(
                [entry], target, import_key="bad", expected_engine="novelai"
            )
        entry["overrides"]["generation_engine"] = "comfyui"
        with pytest.raises(ValueError, match="相同的已知来源"):
            await store.append_import_group(
                [entry], target, import_key="bad", expected_engine="novelai"
            )
        with pytest.raises(ValueError, match="来源不一致"):
            await store.append_import_group(
                [entry], target, import_key="bad", expected_engine="comfyui"
            )
        entry["overrides"]["generation_engine"] = "novelai"
        with store._connect() as conn:
            conn.execute(
                "UPDATE generations SET source='webui' WHERE id = ?", (target,)
            )
        with pytest.raises(ValueError, match="只能合并"):
            await store.append_import_group(
                [entry], target, import_key="bad", expected_engine="novelai"
            )
        with pytest.raises(ValueError, match="不存在或已被删除"):
            await store.append_import_group(
                [entry], "0" * 32, import_key="bad", expected_engine="novelai"
            )
        assert len(list(store.assets_dir.rglob("*.png"))) == 1
        assert len((await store.generation_detail(target))["images"]) == 1

    asyncio.run(run())


def test_append_receipts_survive_restart_and_partial_deletion(tmp_path):
    async def run():
        store = GenerationStore(tmp_path)
        await store.initialize()
        original_entries = [
            stage(store, image(), "original-a.png", model="same"),
            stage(store, image("blue"), "original-b.png", model="same"),
        ]
        target = (
            await store.import_group(original_entries, import_key="original-group")
        )["generation_id"]
        before = await store.generation_detail(target, include_assets=False)
        entries = [
            stage(store, image("green"), "new-a.png"),
            stage(store, image("red"), "new-b.png"),
        ]
        await store.append_import_group(
            entries, target, import_key="merge", expected_engine="novelai"
        )
        reopened = GenerationStore(tmp_path)
        await reopened.initialize()
        retry = await reopened.append_import_group(
            entries, target, import_key="merge", expected_engine="novelai"
        )
        assert retry["duplicate"] and retry["added"] == 0 and retry["image_count"] == 4
        assert (
            await reopened.import_group(original_entries, import_key="original-group")
        )["duplicate"]
        after = await reopened.generation_detail(target, include_assets=False)
        assert (
            after["supplemental"]["group_manifest"]
            == before["supplemental"]["group_manifest"]
        )
        await reopened.delete_images(target, [after["images"][2]["id"]])
        retry = await reopened.append_import_group(
            entries, target, import_key="merge", expected_engine="novelai"
        )
        assert retry["duplicate"] and retry["image_count"] == 3
        with pytest.raises(ValueError, match="顺序或参数"):
            await reopened.append_import_group(
                list(reversed(entries)),
                target,
                import_key="merge",
                expected_engine="novelai",
            )
        entries[0]["overrides"]["prompt"] = "different manual prompt"
        with pytest.raises(ValueError, match="顺序或参数"):
            await reopened.append_import_group(
                entries, target, import_key="merge", expected_engine="novelai"
            )

    asyncio.run(run())


def test_append_duplicate_bytes_keep_separate_links_and_existing_asset(tmp_path):
    async def run():
        store = GenerationStore(tmp_path)
        await store.initialize()
        data = image()
        target = (await store.import_image(data, "first.png", {}))["generation_id"]
        entries = [
            stage(store, data, "repeat-a.png", prompt="A"),
            stage(store, data, "repeat-b.png", prompt="B"),
        ]
        await store.append_import_group(
            entries, target, import_key="repeats", expected_engine="novelai"
        )
        detail = await store.generation_detail(target, include_assets=False)
        assert (
            len(detail["images"]) == 3
            and len({item["id"] for item in detail["images"]}) == 3
        )
        assert len(list(store.assets_dir.rglob("*.png"))) == 1
        assert len(list(store.thumbnails_dir.glob("*.webp"))) == 1
        await store.delete_images(
            target, [detail["images"][0]["id"], detail["images"][1]["id"]]
        )
        assert len(list(store.assets_dir.rglob("*.png"))) == 1
        assert (await store.generation_detail(target))["images"][0]["supplemental"][
            "prompt"
        ] == "B"
        single = await store.import_image(data, "separate single.png", {})
        assert single["generation_id"] != target

    asyncio.run(run())


def test_append_failure_rolls_back_database_and_removes_only_new_orphans(
    tmp_path, monkeypatch
):
    async def run():
        store = GenerationStore(tmp_path)
        await store.initialize()
        data = image()
        target = (await store.import_image(data, "target.png", {}))["generation_id"]
        before = await store.generation_detail(target, include_assets=False)
        entries = [
            stage(store, data, "shared.png"),
            stage(store, image("blue"), "new.png"),
        ]

        def fail(conn, generation_id):
            raise sqlite3.IntegrityError("simulated failure")

        monkeypatch.setattr(store, "_refresh_search_sync", fail)
        with pytest.raises(sqlite3.IntegrityError, match="simulated failure"):
            await store.append_import_group(
                entries, target, import_key="failed", expected_engine="novelai"
            )
        assert await store.generation_detail(target, include_assets=False) == before
        assert len(list(store.assets_dir.rglob("*.png"))) == 1
        assert len(list(store.thumbnails_dir.glob("*.webp"))) == 1
        with store._connect() as conn:
            assert conn.execute("SELECT COUNT(*) FROM image_assets").fetchone()[0] == 1

    asyncio.run(run())


def test_append_enforces_total_image_limit_without_partial_addition(tmp_path):
    async def run():
        store = GenerationStore(tmp_path)
        await store.initialize()
        data = image()
        target = (await store.import_image(data, "target.png", {}))["generation_id"]
        entries = [stage(store, data, f"repeat-{index}.png") for index in range(99)]
        result = await store.append_import_group(
            entries, target, import_key="full", expected_engine="novelai"
        )
        assert result["image_count"] == 100
        with pytest.raises(ValueError, match="100 张上限"):
            await store.append_import_group(
                entries[:1], target, import_key="too-many", expected_engine="novelai"
            )
        assert (
            len((await store.generation_detail(target, include_assets=False))["images"])
            == 100
        )
        assert (
            await store.append_import_group(
                entries, target, import_key="full", expected_engine="novelai"
            )
        )["duplicate"]

    asyncio.run(run())


def test_merge_receipts_remain_internal_and_do_not_enter_search_or_exports(tmp_path):
    from astrbot_plugin_image_studio.parameter_exchange import export_parameters

    async def run():
        store = GenerationStore(tmp_path)
        await store.initialize()
        target = (await store.import_image(image(), "target.png", {}))["generation_id"]
        entries = [
            stage(store, image("blue"), "append.png", prompt="searchable manual prompt")
        ]
        operation_id = "internal-receipt-only-895147"
        await store.append_import_group(
            entries, target, import_key=operation_id, expected_engine="novelai"
        )
        with store._connect() as conn:
            row = conn.execute(
                "SELECT supplemental_json, search_text FROM generations WHERE id = ?",
                (target,),
            ).fetchone()
            receipt = json.loads(row["supplemental_json"])["merge_operations"][
                operation_id
            ]
            assert receipt["added"] == 1
            assert operation_id not in row["search_text"]
            assert receipt["fingerprint"] not in row["search_text"]
        detail = await store.generation_detail(target, include_assets=False)
        assert "merge_operations" not in detail["supplemental"]
        assert all(
            "merge_operations" not in item["supplemental"] for item in detail["images"]
        )
        assert operation_id not in json.dumps(detail)
        assert (await store.list_generations({"query": operation_id}))["total"] == 0
        assert (await store.list_generations({"query": "searchable manual prompt"}))[
            "total"
        ] == 1
        for item in detail["images"]:
            exported = export_parameters(
                detail, image_id=item["id"], format_name="studio"
            )
            assert (
                operation_id not in exported["content"]
                and "merge_operations" not in exported["content"]
            )
        with zipfile.ZipFile(await store.export_generations([target])) as archive:
            for name in archive.namelist():
                if name.endswith(".json"):
                    payload = archive.read(name).decode("utf-8")
                    assert (
                        operation_id not in payload
                        and "merge_operations" not in payload
                    )
        reopened = GenerationStore(tmp_path)
        await reopened.initialize()
        assert (
            await reopened.append_import_group(
                entries, target, import_key=operation_id, expected_engine="novelai"
            )
        )["duplicate"]

    asyncio.run(run())


@pytest.mark.parametrize("change", ["source", "deleted"])
def test_append_rechecks_target_after_asset_preparation(tmp_path, monkeypatch, change):
    async def run():
        store = GenerationStore(tmp_path)
        await store.initialize()
        target = (await store.import_image(image(), "target.png", {}))["generation_id"]
        entries = [stage(store, image("blue"), "new.png")]
        prepare_assets = store._prepare_import_assets_sync

        def changed_target(prepared, preview_max_edge, preview_quality):
            result = prepare_assets(prepared, preview_max_edge, preview_quality)
            if change == "deleted":
                store._delete_generation_sync(target)
            else:
                with store._connect() as conn:
                    row = conn.execute(
                        "SELECT id, supplemental_json FROM generation_images WHERE generation_id = ?",
                        (target,),
                    ).fetchone()
                    supplemental = json.loads(row["supplemental_json"])
                    supplemental["generation_engine"] = "comfyui"
                    conn.execute(
                        "UPDATE generation_images SET supplemental_json = ? WHERE id = ?",
                        (json.dumps(supplemental), row["id"]),
                    )
            return result

        monkeypatch.setattr(store, "_prepare_import_assets_sync", changed_target)
        with pytest.raises(ValueError, match="来源不一致|已被删除"):
            await store.append_import_group(
                entries, target, import_key="changed", expected_engine="novelai"
            )
        remaining = await store.generation_detail(target, include_assets=False)
        if change == "deleted":
            assert remaining is None
            assert not list(store.assets_dir.rglob("*.png"))
        else:
            assert len(remaining["images"]) == 1
            assert not remaining["supplemental"].get("merge_operations")
            assert len(list(store.assets_dir.rglob("*.png"))) == 1

    asyncio.run(run())
