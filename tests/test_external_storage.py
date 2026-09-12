from __future__ import annotations

import asyncio
import hashlib
import io
import json
import os
import threading
import zipfile
from pathlib import Path

import pytest
from PIL import Image

from astrbot_plugin_image_studio.config import HistorySettings
from astrbot_plugin_image_studio.models import (
    GeneratedImage,
    GenerationRequest,
    ImageProvider,
)
from astrbot_plugin_image_studio.storage import ExternalDeleteError, GenerationStore


def image_bytes(color="red"):
    output = io.BytesIO()
    Image.new("RGB", (32, 48), color).save(output, "PNG")
    return output.getvalue()


def fingerprint(path):
    stat = path.stat()
    return {
        "size_bytes": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
        "ctime_ns": stat.st_ctime_ns,
        "device": stat.st_dev,
        "inode": stat.st_ino,
    }


async def setup(tmp_path):
    store = GenerationStore(tmp_path / "studio")
    await store.initialize()
    root = tmp_path / "nai" / "image_history"
    root.mkdir(parents=True)
    await store.configure_external_source("nai", "NAI 插件图库", root, True)
    return store, root


async def add(
    store, root, name="nai_1.png", color="red", parameters=None, sidecars=None
):
    path = root / name
    data = image_bytes(color)
    path.write_bytes(data)
    stamp = fingerprint(path)
    result = await store.upsert_external_image(
        "nai",
        name,
        data,
        fingerprint=json.dumps(stamp),
        sidecar_fingerprint=json.dumps(sidecars or {}),
        size_bytes=stamp["size_bytes"],
        mtime_ns=stamp["mtime_ns"],
        parameters=parameters or {},
        created_at=100,
        generation_engine="novelai",
    )
    return result, path


def test_external_gallery_index_reads_originals_and_owns_only_thumbnails(tmp_path):
    async def run():
        store, root = await setup(tmp_path)
        result, path = await add(
            store,
            root,
            parameters={
                "tag": "a red cat",
                "negative": "bad",
                "model": "nai-v4.5",
                "cfg": 0.2,
            },
        )
        assert list(store.assets_dir.rglob("*.png")) == []
        gallery = await store.list_generations({})
        assert gallery["total"] == 1
        item = gallery["items"][0]
        assert item["source"] == "external" and item["generation_engine"] == "novelai"
        assert item["external_source"] == {"id": "nai", "name": "NAI 插件图库"}
        assert item["thumbnail_data_url"].startswith("data:image/webp")
        assert (await store.list_generations({"query": "red cat"}))["total"] == 1
        detail = await store.generation_detail(result["generation_id"])
        assert detail["is_external"] and detail["parameters"]["cfg"] == 0.2
        assert detail["images"][0]["data_url"].startswith("data:image/png")
        assert detail["images"][0]["supplemental"]["prompt"] == "a red cat"
        manifest = await store.generation_detail(result["generation_id"], light=True)
        assert manifest["is_external"] and manifest["images"][0][
            "download_filename"
        ].endswith(".png")
        resolved = await store.gallery_image_file(result["image_id"])
        assert resolved[0] == path
        assert (await store.gallery_image_data(result["image_id"], detail="original"))[
            "data_url"
        ].startswith("data:image/png")
        assert (await store.gallery_image_info(result["image_id"]))["detail_fields"][
            "is_external"
        ]
        assert len(await store.gallery_image_sequence({})) == 1
        archive = await store.export_generations([result["generation_id"]])
        with zipfile.ZipFile(archive) as exported:
            assert any(name.endswith(".png") for name in exported.namelist())
        await store.close()

    asyncio.run(run())


def test_external_favorites_survive_disable_but_not_source_deletion(tmp_path):
    async def run():
        store, root = await setup(tmp_path)
        result, path = await add(store, root)
        await store.set_favorite(result["generation_id"], True)
        staged = await store.stage_gallery_reference(result["image_id"])
        await store.configure_external_source("nai", "NAI 插件图库", root, False)
        assert path.is_file()
        assert (await store.list_generations({}))["total"] == 0
        assert await store.generation_detail(result["generation_id"]) is None
        assert await store.gallery_image_file(result["image_id"]) is None
        assert (
            await store.gallery_image_data(result["image_id"], detail="preview") is None
        )
        assert list(store.thumbnails_dir.glob("*.webp")) == []
        assert (await store.load_staged_references([staged["id"]]))[
            0
        ].data == path.read_bytes()
        await store.configure_external_source("nai", "NAI 插件图库", root, True)
        stamp = fingerprint(path)
        await store.upsert_external_image(
            "nai",
            path.name,
            path.read_bytes(),
            fingerprint=json.dumps(stamp),
            sidecar_fingerprint="{}",
            size_bytes=stamp["size_bytes"],
            mtime_ns=stamp["mtime_ns"],
        )
        assert (await store.list_generations({"favorite": True}))["total"] == 1
        path.unlink()
        assert await store.reconcile_external_source("nai", []) == 1
        assert (await store.list_generations({}))["total"] == 0
        assert (await store.load_staged_references([staged["id"]]))[0].data
        assert list(store.thumbnails_dir.glob("*.webp")) == []
        await store.close()

    asyncio.run(run())


def test_external_assets_never_enter_local_quota_or_local_health_checks(tmp_path):
    async def run():
        store, root = await setup(tmp_path)
        result, path = await add(store, root)
        await store.set_favorite(result["generation_id"], True)
        quota = await store.retention_status(HistorySettings(True, 1, 1, False))
        assert (
            quota["record_count"]
            == quota["size_bytes"]
            == quota["favorite_records"]
            == 0
        )
        report = await store.run_maintenance(
            history=HistorySettings(True, 1, 1, False),
            preview_max_edge=1024,
            preview_quality=80,
        )
        assert report["repaired"]["broken_assets"] == 0
        assert report["stats"]["assets"] == report["stats"]["generations"] == 0
        external = report["stats"]["external_sources"][0]
        assert (
            external["indexed_count"] == 1
            and external["size_bytes"] == path.stat().st_size
        )
        assert external["thumbnail_count"] == 1
        assert path.is_file()
        await store.close()

    asyncio.run(run())


def test_explicit_external_delete_removes_original_and_matching_sidecars(tmp_path):
    async def run():
        store, root = await setup(tmp_path)
        sidecar = root / "nai_1.yaml"
        sidecar.write_text("tag: cat\n")
        result, path = await add(
            store, root, sidecars={sidecar.name: fingerprint(sidecar)}
        )
        preview = await store.external_delete_preview([result["generation_id"]])
        assert preview == {"external_count": 1, "external_sources": ["NAI 插件图库"]}
        deleted = await store.delete_images(
            result["generation_id"], [result["image_id"]]
        )
        assert (
            deleted["generation_deleted"] and not path.exists() and not sidecar.exists()
        )
        assert (await store.list_generations({}))["total"] == 0
        await store.close()

    asyncio.run(run())


def test_external_delete_refuses_replacement_and_changed_sidecars(tmp_path):
    async def run():
        store, root = await setup(tmp_path)
        sidecar = root / "nai_1.yaml"
        sidecar.write_text("tag: cat\n")
        result, path = await add(
            store, root, sidecars={sidecar.name: fingerprint(sidecar)}
        )
        sidecar.write_text("tag: new cat\n")
        with pytest.raises(ExternalDeleteError, match="参数文件已经变化"):
            await store.delete_generation(result["generation_id"])
        assert path.exists() and sidecar.exists()
        original_time = path.stat().st_mtime_ns
        path.write_bytes(image_bytes("blue"))
        os.utime(path, ns=(original_time, original_time))
        with pytest.raises(ExternalDeleteError):
            await store.delete_generation(result["generation_id"])
        assert path.exists()
        assert await store.gallery_image_file(result["image_id"]) is None
        await store.close()

    asyncio.run(run())


def test_external_missing_original_deletion_removes_index_without_touching_sidecar(
    tmp_path,
):
    async def run():
        store, root = await setup(tmp_path)
        sidecar = root / "nai_1.yaml"
        sidecar.write_text("tag: cat\n")
        result, path = await add(
            store, root, sidecars={sidecar.name: fingerprint(sidecar)}
        )
        path.unlink()
        assert await store.delete_generation(result["generation_id"])
        assert sidecar.exists()
        await store.close()

    asyncio.run(run())


def test_external_path_escape_and_symlink_are_rejected(tmp_path):
    async def run():
        store, root = await setup(tmp_path)
        outside = tmp_path / "outside.png"
        outside.write_bytes(image_bytes())
        link = root / "nai_link.png"
        link.symlink_to(outside)
        stamp = fingerprint(outside)
        for name in (link.name, "../../outside.png", str(outside)):
            with pytest.raises(ValueError):
                await store.upsert_external_image(
                    "nai",
                    name,
                    outside.read_bytes(),
                    fingerprint=json.dumps(stamp),
                    sidecar_fingerprint="{}",
                    size_bytes=stamp["size_bytes"],
                    mtime_ns=stamp["mtime_ns"],
                )
        assert (await store.list_generations({}))["total"] == 0
        assert outside.exists()
        await store.close()

    asyncio.run(run())


def test_owned_duplicate_wins_and_external_delete_preserves_local_copy(tmp_path):
    async def run():
        store, root = await setup(tmp_path)
        external, path = await add(store, root)
        provider = ImageProvider.from_mapping(
            {
                "id": "test",
                "name": "Test",
                "kind": "openai_images",
                "base_url": "https://example.test",
                "model": "model",
            }
        )
        local_id = await store.record_success(
            provider=provider,
            request=GenerationRequest(
                mode="text2img", provider_id="test", prompt="cat"
            ),
            images=(GeneratedImage(path.read_bytes(), "image/png"),),
            elapsed_ms=1,
            history=HistorySettings(True, 10, 10, True),
        )
        listing = await store.list_generations({})
        assert listing["total"] == 1 and listing["items"][0]["id"] == local_id
        assert len(await store.gallery_image_sequence({})) == 1
        local_image = listing["items"][0]["image_id"]
        resolved = await store.gallery_image_file(local_image)
        assert resolved[0].is_relative_to(store.assets_dir)
        assert await store.delete_generation(external["generation_id"])
        assert not path.exists() and resolved[0].exists()
        assert await store.gallery_image_file(local_image)
        await store.close()

    asyncio.run(run())


def test_replaced_external_file_rotates_image_identity_and_keeps_favorite(tmp_path):
    async def run():
        store, root = await setup(tmp_path)
        old, _ = await add(store, root)
        await store.set_favorite(old["generation_id"], True)
        new, _ = await add(store, root, color="blue")
        assert old["generation_id"] == new["generation_id"]
        assert old["image_id"] != new["image_id"]
        assert await store.gallery_image_file(old["image_id"]) is None
        assert (await store.list_generations({"favorite": True}))["items"][0][
            "image_id"
        ] == new["image_id"]
        assert not (store.thumbnails_dir / f"{old['asset_id']}.webp").exists()
        await store.close()

    asyncio.run(run())


def test_partial_external_delete_reports_removed_record_and_remaining_sidecar(
    tmp_path, monkeypatch
):
    async def run():
        store, root = await setup(tmp_path)
        sidecar = root / "nai_1.yaml"
        sidecar.write_text("tag: cat\n")
        result, original = await add(
            store, root, sidecars={sidecar.name: fingerprint(sidecar)}
        )
        unlink = Path.unlink

        def fail_sidecar(path, *args, **kwargs):
            if path == sidecar:
                raise PermissionError("sidecar denied")
            return unlink(path, *args, **kwargs)

        monkeypatch.setattr(Path, "unlink", fail_sidecar)
        with pytest.raises(ExternalDeleteError) as error:
            await store.delete_generation(result["generation_id"])
        assert error.value.as_dict()["generation_deleted"] is True
        assert error.value.as_dict()["deleted_files"] == [original.name]
        assert not original.exists() and sidecar.exists()
        assert (await store.list_generations({}))["total"] == 0
        await store.close()

    asyncio.run(run())


def test_missing_sidecar_mid_delete_does_not_skip_other_sidecars(tmp_path, monkeypatch):
    async def run():
        store, root = await setup(tmp_path)
        yaml_file = root / "nai_1.yaml"
        json_file = root / "nai_1.json"
        yaml_file.write_text("tag: cat\n")
        json_file.write_text('{"tag":"cat"}')
        result, original = await add(
            store,
            root,
            sidecars={
                yaml_file.name: fingerprint(yaml_file),
                json_file.name: fingerprint(json_file),
            },
        )
        unlink = Path.unlink

        def source_cleanup_race(path, *args, **kwargs):
            value = unlink(path, *args, **kwargs)
            if path == original:
                unlink(yaml_file)
            return value

        monkeypatch.setattr(Path, "unlink", source_cleanup_race)
        assert await store.delete_generation(result["generation_id"])
        assert (
            not original.exists() and not yaml_file.exists() and not json_file.exists()
        )
        await store.close()

    asyncio.run(run())


def test_cancelled_scan_mutation_finishes_before_disable_returns(tmp_path, monkeypatch):
    async def run():
        store, root = await setup(tmp_path)
        started = threading.Event()
        release = threading.Event()
        upsert = store._upsert_external_image_sync

        def paused_upsert(*args):
            started.set()
            assert release.wait(5)
            return upsert(*args)

        monkeypatch.setattr(store, "_upsert_external_image_sync", paused_upsert)
        mutation = asyncio.create_task(add(store, root))
        assert await asyncio.to_thread(started.wait, 5)
        mutation.cancel()
        disable = asyncio.create_task(
            store.configure_external_source("nai", "NAI 插件图库", root, False)
        )
        await asyncio.sleep(0)
        assert not disable.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await mutation
        await disable
        assert (await store.list_generations({}))["total"] == 0
        assert list(store.thumbnails_dir.glob("*.webp")) == []
        await store.close()

    asyncio.run(run())


def test_disabled_external_index_does_not_block_owned_import(tmp_path):
    async def run():
        store, root = await setup(tmp_path)
        external, path = await add(store, root)
        data = path.read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        assert not (await store.check_import_hashes([digest]))["allowed"]
        await store.configure_external_source("nai", "NAI 插件图库", root, False)
        assert (await store.check_import_hashes([digest]))["allowed"]
        imported = await store.import_image(data, path.name, {})
        await store.configure_external_source("nai", "NAI 插件图库", root, True)
        items = (await store.list_generations({}))["items"]
        assert len(items) == 1 and items[0]["id"] == imported["generation_id"]
        assert (await store.gallery_image_file(items[0]["image_id"]))[0].is_relative_to(
            store.assets_dir
        )
        await store.delete_generation(imported["generation_id"])
        assert path.exists() and not list(store.assets_dir.rglob("*.png"))
        assert (await store.list_generations({}))["items"][0]["id"] == external[
            "generation_id"
        ]
        assert (await store.gallery_image_file(external["image_id"]))[0] == path
        await store.close()

    asyncio.run(run())


def test_deleting_local_duplicate_reveals_external_without_retaining_owned_copy(
    tmp_path,
):
    async def run():
        store, root = await setup(tmp_path)
        external, path = await add(store, root)
        provider = ImageProvider.from_mapping(
            {
                "id": "test",
                "kind": "openai_images",
                "base_url": "https://example.test",
                "model": "model",
            }
        )
        local = await store.record_success(
            provider=provider,
            request=GenerationRequest(
                mode="text2img", provider_id="test", prompt="cat"
            ),
            images=(GeneratedImage(path.read_bytes(), "image/png"),),
            elapsed_ms=1,
            history=HistorySettings(True, 10, 10, True),
        )
        assert list(store.assets_dir.rglob("*.png"))
        await store.delete_generation(local)
        assert not list(store.assets_dir.rglob("*.png")) and path.exists()
        assert (await store.list_generations({}))["items"][0]["id"] == external[
            "generation_id"
        ]
        assert (await store.gallery_image_file(external["image_id"]))[0] == path
        await store.close()

    asyncio.run(run())
