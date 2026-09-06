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
from astrbot_plugin_image_studio.storage import GenerationStore, ImportDuplicateError


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


def comfy_multi_output_image(color="red"):
    graph = {}
    for offset, prompt in ((0, "first branch prompt"), (10, "second branch prompt")):
        graph.update(
            {
                str(offset + 1): {
                    "class_type": "CLIPTextEncode",
                    "inputs": {"text": prompt},
                },
                str(offset + 2): {
                    "class_type": "CLIPTextEncode",
                    "inputs": {"text": "negative " + prompt},
                },
                str(offset + 3): {
                    "class_type": "EmptyLatentImage",
                    "inputs": {"width": 512, "height": 512},
                },
                str(offset + 4): {
                    "class_type": "KSampler",
                    "inputs": {
                        "positive": [str(offset + 1), 0],
                        "negative": [str(offset + 2), 0],
                        "latent_image": [str(offset + 3), 0],
                        "steps": offset + 20,
                        "seed": offset + 1,
                    },
                },
                str(offset + 5): {
                    "class_type": "VAEDecode",
                    "inputs": {"samples": [str(offset + 4), 0]},
                },
                str(offset + 6): {
                    "class_type": "SaveImage",
                    "inputs": {"images": [str(offset + 5), 0]},
                },
            }
        )
    graph["99"] = {"class_type": "PreviewImage", "inputs": {"images": ["5", 0]}}
    raw = {
        "prompt": json.dumps(graph),
        "workflow": json.dumps({"nodes": [], "links": []}),
    }
    info = PngImagePlugin.PngInfo()
    for key, value in raw.items():
        info.add_text(key, value)
    output = io.BytesIO()
    Image.new("RGB", (24, 32), color).save(output, "PNG", pnginfo=info)
    return output.getvalue(), raw


def test_comfy_output_selection_is_required_and_link_scoped(tmp_path):
    async def run():
        store = GenerationStore(tmp_path)
        await store.initialize()
        data, raw = comfy_multi_output_image()
        with pytest.raises(ValueError, match="多个.*保存"):
            await store.import_image(data, "multiple.png", {})
        for invalid in ("99", "missing", 6):
            with pytest.raises(ValueError):
                await store.import_image(
                    data, "invalid.png", {"comfy_output_node": invalid}
                )
        first = await store.import_image(
            data, "first.png", {"comfy_output_node": "6"}, import_key="first-branch"
        )
        with pytest.raises(ImportDuplicateError):
            await store.import_image(
                data, "duplicate-branch.png", {"comfy_output_node": "16"}
            )
        second = await store.import_image(
            comfy_multi_output_image("blue")[0],
            "second.png",
            {
                "comfy_output_node": "16",
                "prompt": "manual adopted text",
                "parameters": {"steps": 12},
            },
        )
        assert first["generation_id"] != second["generation_id"]
        assert len(list(store.assets_dir.rglob("*.png"))) == 2
        for result, selected, expected in (
            (first, "6", "first branch prompt"),
            (second, "16", "second branch prompt"),
        ):
            detail = await store.generation_detail(
                result["generation_id"], include_assets=False
            )
            item = detail["images"][0]
            assert item["metadata"]["normalized"]["selected_output_node"] == selected
            assert item["metadata"]["normalized"]["prompt"] == expected
            assert item["metadata"]["raw"] == raw
            assert item["supplemental"]["comfy_output_node"] == selected
            assert {
                candidate["output_node_ids"][0]
                for candidate in item["metadata"]["normalized"]["prompt_candidates"]
            } == {selected}
        second_detail = await store.generation_detail(
            second["generation_id"], include_assets=False
        )
        assert second_detail["original_prompt"] == "manual adopted text"
        assert (
            second_detail["images"][0]["supplemental"]["display_parameters"]["steps"]
            == 12
        )
        with store._connect() as conn:
            shared = json.loads(
                conn.execute("SELECT metadata_json FROM image_metadata").fetchone()[0]
            )
        assert shared["normalized"]["requires_output_selection"] is True
        assert not shared["normalized"].get("prompt") and not shared["normalized"].get(
            "selected_output_node"
        )
        assert shared["raw"] == raw
        with pytest.raises(ValueError):
            await store.import_image(
                data,
                "wrong-branch.png",
                {"comfy_output_node": "16"},
                import_key="first-branch",
            )

    asyncio.run(run())


def test_legacy_multisave_import_survives_parser_upgrade_without_automatic_branch_choice(
    tmp_path, monkeypatch
):
    import astrbot_plugin_image_studio.image_metadata as metadata_module

    async def run():
        data, raw = comfy_multi_output_image()
        current_version = metadata_module.PARSER_VERSION
        current_parser = metadata_module.parse_image_metadata

        def old_parser(data):
            return {
                "format": "comfyui",
                "parser_version": current_version - 1,
                "raw": raw,
                "normalized": {"prompt": "old combined prompt", "mode": "unknown"},
                "warnings": [],
            }

        monkeypatch.setattr(metadata_module, "PARSER_VERSION", current_version - 1)
        monkeypatch.setattr(metadata_module, "parse_image_metadata", old_parser)
        store = GenerationStore(tmp_path)
        await store.initialize()
        imported = await store.import_image(
            data, "legacy.png", {"prompt": "legacy explicit prompt"}
        )
        before = await store.generation_detail(
            imported["generation_id"], include_assets=False
        )
        monkeypatch.setattr(metadata_module, "PARSER_VERSION", current_version)
        monkeypatch.setattr(metadata_module, "parse_image_metadata", current_parser)
        await GenerationStore(tmp_path).initialize()
        after = await store.generation_detail(
            imported["generation_id"], include_assets=False
        )
        assert (
            after["original_prompt"]
            == before["original_prompt"]
            == "legacy explicit prompt"
        )
        assert after["images"][0]["supplemental"] == before["images"][0]["supplemental"]
        assert after["images"][0]["metadata"]["normalized"]["requires_output_selection"]
        assert not after["images"][0]["metadata"]["normalized"].get("prompt_candidates")
        assert len(list(store.assets_dir.rglob("*.png"))) == 1

    asyncio.run(run())


def test_comfy_group_retry_rejects_changed_output_selection(tmp_path):
    async def run():
        store = GenerationStore(tmp_path)
        await store.initialize()
        data, _ = comfy_multi_output_image()
        entries = [
            stage(store, data, "first.png", model="same model", comfy_output_node="6"),
            stage(
                store,
                comfy_multi_output_image("blue")[0],
                "second.png",
                model="same model",
                comfy_output_node="16",
            ),
        ]
        imported = await store.import_group(entries, import_key="branch-group")
        detail = await store.generation_detail(
            imported["generation_id"], include_assets=False
        )
        assert [
            item["metadata"]["normalized"]["selected_output_node"]
            for item in detail["images"]
        ] == ["6", "16"]
        assert (await store.import_group(entries, import_key="branch-group"))[
            "duplicate"
        ]
        entries[1]["overrides"]["comfy_output_node"] = "6"
        with pytest.raises(ValueError):
            await store.import_group(entries, import_key="branch-group")

    asyncio.run(run())


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
        assert detail["mode"] == "unknown" and detail["generation_engine"] == "novelai"
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
        with pytest.raises(ValueError, match="顺序或参数"):
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


def test_group_rejects_repeated_bytes_without_overwriting_image_parameters(tmp_path):
    async def run():
        store = GenerationStore(tmp_path)
        await store.initialize()
        data = image()
        entries = [
            stage(store, data, "one.png", prompt="one"),
            stage(store, data, "two.png", prompt="two"),
        ]
        with pytest.raises(ImportDuplicateError) as duplicate:
            await store.import_group(entries, import_key="group")
        assert duplicate.value.code == "batch_duplicates"
        assert (await store.list_generations({}))["total"] == 0
        assert not list(store.assets_dir.rglob("*.png"))
        first = await store.import_image(data, "first.png", {"prompt": "original"})
        with pytest.raises(ImportDuplicateError):
            await store.import_group([entries[0]], import_key="second-group")
        assert (await store.generation_detail(first["generation_id"]))[
            "original_prompt"
        ] == "original"
        await store.delete_generation(first["generation_id"])
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
            stage(store, image("yellow"), "one.png"),
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


def test_older_development_layout_is_rejected_without_rewriting_imports(tmp_path):
    async def run():
        store = GenerationStore(tmp_path)
        await store.initialize()
        imported = await store.import_image(
            image(), "one.png", {"prompt": "manual", "parameters": {"artist": "artist"}}
        )
        await store.set_favorite(imported["generation_id"], True)
        with store._connect() as conn:
            conn.execute("ALTER TABLE generation_images DROP COLUMN supplemental_json")
            conn.execute(
                "CREATE TABLE schema_meta (id INTEGER PRIMARY KEY, target_version INTEGER NOT NULL, dev_revision INTEGER NOT NULL)"
            )
            conn.execute("INSERT INTO schema_meta VALUES (1, 1, 1)")
            conn.execute("PRAGMA user_version = 0")
        with store._connect() as conn:
            before = list(conn.iterdump())
        upgraded = GenerationStore(tmp_path)
        with pytest.raises(RuntimeError, match="末版开发版"):
            await upgraded.initialize()
        with upgraded._connect() as conn:
            assert list(conn.iterdump()) == before
            assert conn.execute("PRAGMA user_version").fetchone()[0] == 0
        assert not (tmp_path / "backups").exists()

    asyncio.run(run())


def test_nai_source_aliases_share_filters_and_preserve_raw_import_information(tmp_path):
    async def run():
        store = GenerationStore(tmp_path)
        await store.initialize()
        entries = [
            stage(store, image(), "one.png", generation_engine="nai"),
            stage(store, image("blue"), "two.png", generation_engine="novelai"),
        ]
        grouped = await store.import_group(entries, import_key="aliases")
        single = await store.import_image(
            image("green"), "single.png", {"generation_engine": "nai"}
        )
        group_id, single_id = grouped["generation_id"], single["generation_id"]
        detail = await store.generation_detail(group_id, include_assets=False)
        assert detail["generation_engine"] == "novelai"
        assert {
            item["supplemental"]["generation_engine"] for item in detail["images"]
        } == {"novelai"}
        assert (
            detail["images"][0]["supplemental"]["overrides"]["generation_engine"]
            == "nai"
        )
        assert detail["images"][0]["metadata"]["raw"]["Software"] == "NovelAI"
        with store._connect() as conn:
            assert {
                row[0]
                for row in conn.execute("SELECT generation_engine FROM generations")
            } == {"novelai"}
            conn.execute(
                "UPDATE generations SET generation_engine = 'nai' WHERE id = ?",
                (single_id,),
            )
        for alias in ("nai", "novelai", "NovelAI"):
            gallery = await store.list_generations({"generation_engine": alias})
            assert gallery["total"] == 2
            assert gallery["filters"]["generation_engines"] == ["novelai"]
            assert {item["generation_engine"] for item in gallery["items"]} == {
                "novelai"
            }
            assert (
                len(await store.gallery_image_sequence({"generation_engine": alias}))
                == 3
            )

        # Simulate old group-level and per-image aliases without rewriting user inputs.
        with store._connect() as conn:
            conn.execute(
                "UPDATE generations SET generation_engine = 'mixed' WHERE id = ?",
                (group_id,),
            )
            for row in conn.execute(
                "SELECT id, supplemental_json FROM generation_images"
            ).fetchall():
                supplemental = json.loads(row["supplemental_json"])
                if supplemental["overrides"]["generation_engine"] == "nai":
                    supplemental["generation_engine"] = "nai"
                    supplemental["display_parameters"]["generation_engine"] = "nai"
                conn.execute(
                    "UPDATE generation_images SET supplemental_json = ? WHERE id = ?",
                    (json.dumps(supplemental), row["id"]),
                )
            original_supplementals = [
                tuple(row)
                for row in conn.execute(
                    "SELECT id, supplemental_json FROM generation_images ORDER BY id"
                )
            ]
            original_metadata = [
                tuple(row)
                for row in conn.execute(
                    "SELECT asset_id, metadata_json FROM image_metadata ORDER BY asset_id"
                )
            ]
        reopened = GenerationStore(tmp_path)
        await reopened.initialize()
        gallery = await reopened.list_generations({"generation_engine": "novelai"})
        assert gallery["total"] == 2
        detail = await reopened.generation_detail(group_id, include_assets=False)
        assert detail["generation_engine"] == "novelai"
        assert detail["images"][0]["supplemental"]["generation_engine"] == "novelai"
        assert (
            detail["images"][0]["supplemental"]["display_parameters"][
                "generation_engine"
            ]
            == "novelai"
        )
        assert (
            detail["images"][0]["supplemental"]["overrides"]["generation_engine"]
            == "nai"
        )
        with reopened._connect() as conn:
            assert [
                tuple(row)
                for row in conn.execute(
                    "SELECT id, supplemental_json FROM generation_images ORDER BY id"
                )
            ] == original_supplementals
            assert [
                tuple(row)
                for row in conn.execute(
                    "SELECT asset_id, metadata_json FROM image_metadata ORDER BY asset_id"
                )
            ] == original_metadata
            assert conn.execute("PRAGMA user_version").fetchone()[0] == 1

    asyncio.run(run())


def test_distinct_generation_engines_remain_mixed_after_alias_normalization(tmp_path):
    async def run():
        store = GenerationStore(tmp_path)
        await store.initialize()
        grouped = await store.import_group(
            [
                stage(store, image(), "one.png", generation_engine="nai"),
                stage(store, image("blue"), "two.png", generation_engine="comfyui"),
            ],
            import_key="mixed",
        )
        await store.initialize()
        assert (
            await store.generation_detail(
                grouped["generation_id"], include_assets=False
            )
        )["generation_engine"] == "mixed"
        assert (await store.list_generations({"generation_engine": "mixed"}))[
            "total"
        ] == 1
        assert (await store.list_generations({"generation_engine": "novelai"}))[
            "total"
        ] == 0

    asyncio.run(run())


def test_metadata_upgrade_refreshes_group_projection_and_keeps_manifest_and_manual_overrides(
    tmp_path, monkeypatch
):
    import astrbot_plugin_image_studio.image_metadata as metadata_module

    async def run():
        store = GenerationStore(tmp_path)
        await store.initialize()
        entries = [
            stage(store, image(seed=1), "one.png", model="confirmed model"),
            stage(
                store,
                image("blue", seed=2),
                "two.png",
                model="confirmed model",
                prompt="",
                mode="img2img",
                generation_engine="comfyui",
                parameters={"seed": 0},
            ),
        ]
        grouped = await store.import_group(entries, import_key="upgraded")
        await store.set_favorite(grouped["generation_id"], True)
        before = await store.generation_detail(
            grouped["generation_id"], include_assets=False
        )
        parse = metadata_module.parse_image_metadata
        upgraded_version = metadata_module.PARSER_VERSION + 1

        def upgraded(data):
            result = parse(data)
            result["normalized"].update(
                prompt=f"resolved {result['normalized']['seed']}",
                model="new metadata model",
                mode="text2img",
                steps=35,
            )
            return result

        monkeypatch.setattr(metadata_module, "PARSER_VERSION", upgraded_version)
        monkeypatch.setattr(metadata_module, "parse_image_metadata", upgraded)
        await GenerationStore(tmp_path).initialize()
        after = await store.generation_detail(
            grouped["generation_id"], include_assets=False
        )
        assert after["model"] == "confirmed model"
        assert after["original_prompt"] == "resolved 1" and after["mode"] == "unknown"
        assert after["generation_engine"] == "mixed"
        assert after["created_at"] == before["created_at"] and after["is_favorite"]
        assert [item["id"] for item in after["images"]] == [
            item["id"] for item in before["images"]
        ]
        assert (
            after["supplemental"]["group_manifest"]
            == before["supplemental"]["group_manifest"]
        )
        assert after["supplemental"]["is_import_group"] is True
        assert [item["supplemental"]["prompt"] for item in after["images"]] == [
            "resolved 1",
            "",
        ]
        assert after["images"][1]["supplemental"]["display_parameters"]["seed"] == 0
        assert after["images"][1]["supplemental"]["display_parameters"]["steps"] == 35
        for old, new in zip(before["images"], after["images"], strict=True):
            assert new["supplemental"]["overrides"] == old["supplemental"]["overrides"]
            assert new["metadata"]["raw"] == old["metadata"]["raw"]
        assert (await store.import_group(entries, import_key="upgraded"))["duplicate"]

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
