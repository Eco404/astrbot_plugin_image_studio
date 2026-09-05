from __future__ import annotations

import asyncio
import io
import json
import os
import time
import zipfile

import pytest
from PIL import Image, PngImagePlugin

from astrbot_plugin_image_studio.config import HistorySettings
from astrbot_plugin_image_studio.storage import GenerationStore


def image(color="red", *, prompt="embedded", model="embedded-model", seed=1):
    output = io.BytesIO()
    info = PngImagePlugin.PngInfo()
    info.add_text("Software", "NovelAI")
    info.add_text(
        "Comment",
        json.dumps({"prompt": prompt, "model": model, "seed": seed, "uc": "blur"}),
    )
    Image.new("RGB", (24, 32), color).save(output, "PNG", pnginfo=info)
    return output.getvalue()


def stage(store, data, name, **overrides):
    path = store.imports_dir / name
    path.write_bytes(data)
    return {"path": path, "filename": name, "overrides": overrides}


def test_import_group_preserves_per_image_metadata_and_overrides(tmp_path):
    async def run():
        store = GenerationStore(tmp_path)
        await store.initialize()
        entries = [
            stage(
                store,
                image(seed=111),
                "one.png",
                model=" model-a ",
                prompt="first manual",
                mode="text2img",
                generation_engine="nai",
                parameters={"artist": "first artist"},
            ),
            stage(
                store,
                image("blue", seed=222, model="different embedded"),
                "two.png",
                model="model-a",
                prompt="second manual",
                negative_prompt="second negative",
                mode="img2img",
                generation_engine="novelai",
                parameters={"artist": "second artist", "steps": 10},
            ),
        ]
        result = await store.import_group(entries, import_key="batch")
        detail = await store.generation_detail(
            result["generation_id"], include_assets=False
        )
        assert detail["model"] == "model-a"
        assert detail["mode"] == "unknown" and detail["generation_engine"] == "mixed"
        assert detail["parameters"] == {} and detail["source"] == "import"
        assert detail["original_prompt"] == "first manual"
        assert [
            item["metadata"]["normalized"]["seed"] for item in detail["images"]
        ] == [111, 222]
        assert [item["supplemental"]["prompt"] for item in detail["images"]] == [
            "first manual",
            "second manual",
        ]
        second = detail["images"][1]["supplemental"]
        assert "_t2i_" in detail["images"][0]["download_filename"]
        assert "_i2i_" in detail["images"][1]["download_filename"]
        sequence = await store.gallery_image_sequence({})
        assert [item["download_filename"] for item in sequence] == [
            item["download_filename"] for item in detail["images"]
        ]
        assert second["overrides"]["parameters"]["steps"] == 10
        assert second["display_parameters"]["artist"] == "second artist"
        assert second["negative_prompt"] == "second negative"
        assert (await store.list_generations({"query": "second manual"}))["total"] == 1
        assert (await store.list_generations({"query": "second artist"}))["total"] == 1
        assert (await store.gallery_image_sequence({"query": "second artist"}))[1][
            "image_index"
        ] == 1
        with zipfile.ZipFile(await store.export_generations([detail["id"]])) as archive:
            assert detail["images"][1]["download_filename"] in archive.namelist()
            values = [
                json.loads(archive.read(name))
                for name in archive.namelist()
                if name.endswith(".json")
            ]
        assert values[1]["original_prompt"] == "second manual"
        assert (
            values[1]["mode"] == "img2img"
            and values[1]["generation_engine"] == "novelai"
        )
        assert values[1]["image"]["supplemental"] == second
        assert values[1]["image"]["metadata"]["normalized"]["seed"] == 222

    asyncio.run(run())


@pytest.mark.parametrize("models", [("model-a", "model-b"), ("", ""), ("model-a", "")])
def test_group_requires_identical_nonempty_confirmed_models(tmp_path, models):
    async def run():
        store = GenerationStore(tmp_path)
        await store.initialize()
        entries = [
            stage(store, image(), "one.png", model=models[0]),
            stage(store, image("blue"), "two.png", model=models[1]),
        ]
        with pytest.raises(ValueError, match="模型必须全部填写且完全相同") as failure:
            await store.import_group(entries, import_key="group")
        assert "one.png" in str(failure.value) and "two.png" in str(failure.value)
        assert (await store.list_generations({}))["total"] == 0
        assert not list(store.assets_dir.rglob("*.png"))

    asyncio.run(run())


def test_group_retry_uses_original_ordered_manifest_after_partial_delete(tmp_path):
    async def run():
        store = GenerationStore(tmp_path)
        await store.initialize()
        entries = [
            stage(store, image(), "one.png"),
            stage(store, image("blue"), "two.png"),
        ]
        created = await store.import_group(entries, import_key="group")
        assert await store.import_group(entries, import_key="group") == {
            "generation_id": created["generation_id"],
            "duplicate": True,
        }
        with pytest.raises(ValueError, match="顺序不同"):
            await store.import_group(list(reversed(entries)), import_key="group")
        detail = await store.generation_detail(
            created["generation_id"], include_assets=False
        )
        await store.delete_images(detail["id"], [detail["images"][0]["id"]])
        assert (await store.import_group(entries, import_key="group"))["duplicate"]
        remaining = await store.generation_detail(detail["id"], include_assets=False)
        assert len(remaining["images"]) == 1
        assert remaining["images"][0]["supplemental"]["original_filename"] == "two.png"
        assert (await store.list_generations({}))["items"][0]["image_id"] == remaining[
            "images"
        ][0]["id"]

    asyncio.run(run())


def test_group_shares_assets_but_keeps_association_data_and_single_import_identity(
    tmp_path,
):
    async def run():
        store = GenerationStore(tmp_path)
        await store.initialize()
        data = image()
        entries = [
            stage(store, data, "one.png", prompt="one"),
            stage(store, data, "two.png", prompt="two"),
        ]
        grouped = await store.import_group(entries, import_key="group")
        assert len(list(store.assets_dir.rglob("*.png"))) == 1
        detail = await store.generation_detail(
            grouped["generation_id"], include_assets=False
        )
        assert [item["supplemental"]["prompt"] for item in detail["images"]] == [
            "one",
            "two",
        ]
        assert detail["images"][0]["sha256"] == detail["images"][1]["sha256"]
        await store.delete_images(detail["id"], [detail["images"][0]["id"]])
        single = await store.import_image(data, "single.png", {"prompt": "single"})
        assert (
            single["generation_id"] != grouped["generation_id"]
            and not single["duplicate"]
        )
        assert (await store.import_image(data, "again.png", {}))[
            "generation_id"
        ] == single["generation_id"]
        with pytest.raises(ValueError, match="另一张图片"):
            await store.import_image(data, "single.png", {}, import_key="group")
        await store.set_favorite(grouped["generation_id"], True)
        status = await store.retention_status(HistorySettings(True, 0, 0, False))
        assert status["record_count"] == status["size_bytes"] == 0
        store._cleanup_sync(HistorySettings(True, 0, 0, False))
        assert (await store.list_generations({}))["total"] == 2
        await store.delete_generation(grouped["generation_id"])
        assert len(list(store.assets_dir.rglob("*.png"))) == 1
        await store.delete_generation(single["generation_id"])
        assert not list(store.assets_dir.rglob("*.png"))

    asyncio.run(run())


def test_group_failure_rolls_back_all_links_and_preserves_shared_assets(
    tmp_path, monkeypatch
):
    async def run():
        store = GenerationStore(tmp_path)
        await store.initialize()
        data = image()
        existing = await store.import_image(data, "existing.png", {})
        entries = [
            stage(store, data, "one.png"),
            stage(store, image("blue"), "two.png"),
        ]

        def fail(*args):
            raise RuntimeError("simulated transaction failure")

        monkeypatch.setattr(store, "_refresh_search_sync", fail)
        with pytest.raises(RuntimeError, match="transaction failure"):
            await store.import_group(entries, import_key="group")
        gallery = await store.list_generations({})
        assert (
            gallery["total"] == 1
            and gallery["items"][0]["id"] == existing["generation_id"]
        )
        assert len(list(store.assets_dir.rglob("*.png"))) == 1
        assert len(list(store.thumbnails_dir.glob("*.webp"))) == 1
        with store._connect() as conn:
            assert conn.execute("SELECT COUNT(*) FROM image_assets").fetchone()[0] == 1
            assert (
                conn.execute("SELECT COUNT(*) FROM generation_images").fetchone()[0]
                == 1
            )

    asyncio.run(run())


def test_group_rejects_paths_outside_staging_and_bad_members(tmp_path):
    async def run():
        store = GenerationStore(tmp_path / "store")
        await store.initialize()
        outside = tmp_path / "outside.png"
        outside.write_bytes(image())
        linked = store.imports_dir / "linked.png"
        linked.symlink_to(outside)
        good = stage(store, image(), "one.png")
        for path in (outside, linked):
            with pytest.raises(ValueError, match="不属于导入暂存目录"):
                await store.import_group(
                    [good, {"path": path, "filename": "outside.png", "overrides": {}}],
                    import_key="group",
                )
        bad = stage(store, b"not an image", "bad.png")
        with pytest.raises(ValueError, match="bad.png"):
            await store.import_group([good, bad], import_key="group")
        assert (await store.list_generations({}))["total"] == 0
        assert not list(store.assets_dir.rglob("*.png"))

    asyncio.run(run())


def test_dev1_upgrade_preserves_single_import_and_metadata(tmp_path):
    async def run():
        store = GenerationStore(tmp_path)
        await store.initialize()
        imported = await store.import_image(
            image(), "one.png", {"prompt": "manual", "parameters": {"artist": "artist"}}
        )
        await store.set_favorite(imported["generation_id"], True)
        before = await store.generation_detail(
            imported["generation_id"], include_assets=False
        )
        with store._connect() as conn:
            conn.execute("ALTER TABLE generation_images DROP COLUMN supplemental_json")
            conn.execute("UPDATE schema_meta SET dev_revision = 1")
        upgraded = GenerationStore(tmp_path)
        await upgraded.initialize()
        after = await upgraded.generation_detail(
            imported["generation_id"], include_assets=False
        )
        assert after["is_favorite"]
        assert after["images"][0]["metadata"] == before["images"][0]["metadata"]
        assert after["images"][0]["supplemental"]["prompt"] == "manual"
        assert (
            after["images"][0]["supplemental"]["overrides"]["parameters"]["artist"]
            == "artist"
        )
        with upgraded._connect() as conn:
            assert (
                conn.execute(
                    "SELECT target_version, dev_revision FROM schema_meta"
                ).fetchone()["dev_revision"]
                == 2
            )
            assert conn.execute("PRAGMA user_version").fetchone()[0] == 0
        await upgraded.initialize()
        assert (
            await upgraded.generation_detail(
                imported["generation_id"], include_assets=False
            )
            == after
        )

    asyncio.run(run())


def test_import_staging_maintenance_uses_one_hour_grace(tmp_path):
    async def run():
        store = GenerationStore(tmp_path)
        await store.initialize()
        old = stage(store, image(), "old.png")["path"]
        recent = stage(store, image(), "recent.png")["path"]
        past = time.time() - 3601
        os.utime(old, (past, past))
        store._cleanup_stale_files_sync(time.time())
        assert not old.exists() and recent.exists()

    asyncio.run(run())


def test_import_staging_recreates_empty_group_directory_and_validates_before_writing(
    tmp_path,
):
    async def run():
        store = GenerationStore(tmp_path / "store")
        await store.initialize()
        group_dir = store.imports_dir / "group"
        group_dir.mkdir()
        await store.run_maintenance(
            HistorySettings(True, -1, 0, False),
            preview_max_edge=768,
            preview_quality=80,
        )
        assert not group_dir.exists()
        staged = group_dir / "image.png"
        data = image()
        await store.stage_import_file(staged, data)
        assert staged.read_bytes() == data
        with pytest.raises(ValueError, match="不属于"):
            await store.stage_import_file(tmp_path / "outside.png", data)
        with pytest.raises(ValueError, match="文件路径"):
            await store.stage_import_file(store.imports_dir, data)
        bad = group_dir / "bad.png"
        with pytest.raises(ValueError, match="无法读取"):
            await store.stage_import_file(bad, b"not an image")
        assert not bad.exists() and not (tmp_path / "outside.png").exists()

    asyncio.run(run())
