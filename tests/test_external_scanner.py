from __future__ import annotations

import asyncio
import hashlib
import io
import json
import threading

import pytest
from astrbot_plugin_image_studio import external_gallery
from astrbot_plugin_image_studio.config import HistorySettings
from astrbot_plugin_image_studio.external_gallery import (
    ExternalGalleryAdapter,
    ExternalGalleryManager,
    NAIGalleryAdapter,
)
from astrbot_plugin_image_studio.image_metadata import PARSER_VERSION
from astrbot_plugin_image_studio.models import (
    GeneratedImage,
    GenerationRequest,
    ImageProvider,
)
from astrbot_plugin_image_studio.storage import GenerationStore
from PIL import Image, PngImagePlugin


class MemoryStore:
    def __init__(self):
        self.sources = {}
        self.images = {}
        self.writes = []
        self.reconciled = []
        self.reports = []

    async def configure_external_source(
        self, source_id, name, root_path, enabled, **settings
    ):
        old = self.sources.get(source_id, {})
        if old and any(
            old.get(key) != value
            for key, value in {
                "root_path": root_path,
                "source_type": settings.get("source_type"),
                "recursive": settings.get("recursive"),
            }.items()
        ):
            self.images.clear()
        self.sources.setdefault(source_id, {}).update(
            id=source_id, name=name, root_path=root_path, enabled=enabled, **settings
        )

    async def remove_external_source(self, source_id):
        self.sources.pop(source_id, None)
        self.images.clear()

    async def external_sources_status(self):
        return [
            {
                **source,
                "indexed_count": len(self.images),
                "size_bytes": sum(
                    value["size_bytes"] for value in self.images.values()
                ),
                "thumbnail_count": len(self.images),
                "thumbnail_bytes": len(self.images) * 10,
            }
            for source in self.sources.values()
        ]

    async def external_scan_snapshot(self, source_id):
        return {key: dict(value) for key, value in self.images.items()}

    async def upsert_external_image(self, source_id, filename, data, **kwargs):
        if not self.sources[source_id]["enabled"]:
            return None
        value = dict(
            kwargs,
            sha256=hashlib.sha256(data).hexdigest(),
            thumbnail_available=True,
            thumbnail_max_edge=kwargs["preview_max_edge"],
            thumbnail_quality=kwargs["preview_quality"],
            parser_version=PARSER_VERSION,
        )
        self.images[filename] = value
        self.writes.append((filename, value))
        return {"generation_id": filename}

    async def reconcile_external_source(self, source_id, seen):
        self.reconciled.append(set(seen))
        self.images = {key: value for key, value in self.images.items() if key in seen}

    async def set_external_status(self, source_id, report):
        self.sources[source_id].update(report)
        self.reports.append(report)


def manager_fixture(tmp_path, **kwargs):
    store = MemoryStore()
    data_dir = tmp_path / "plugin_data" / "astrbot_plugin_image_studio"
    root = data_dir.parent / "astrbot_plugin_nai_image" / "image_history"
    return store, ExternalGalleryManager(store, data_dir, **kwargs), root


async def scan(manager):
    await manager.request_scan("nai")
    await manager._tasks["nai"]
    return (await manager.status())[0]


def test_missing_source_does_not_create_or_clear_history(tmp_path):
    async def run():
        store, manager, root = manager_fixture(tmp_path)
        store.images["nai_old.png"] = {"size_bytes": 10}
        await manager.configure({"nai": True})
        report = await scan(manager)
        assert report["status"] == "unavailable"
        assert report["indexed_count"] == 1
        assert not root.exists()
        assert store.reconciled == []
        assert not list(tmp_path.iterdir())
        await manager.close()

    asyncio.run(run())


def test_incremental_scan_detects_late_and_removed_sidecar_without_overwriting_image(
    tmp_path,
):
    async def run():
        store, manager, root = manager_fixture(tmp_path)
        root.mkdir(parents=True)
        path = root / "nai_1700000000000000000.png"
        path.write_bytes(b"original image bytes")
        (root / "random.png").write_bytes(b"ignored")
        (root / ".nai_1700000000000000000.png.tmp").write_bytes(b"incomplete")
        await manager.configure({"nai": {"enabled": True}})
        first = await scan(manager)
        assert first["status"] == "complete"
        assert first["processed"] == first["total"] == first["indexed_count"] == 1
        assert store.writes[0][1]["created_at"] == 1700000000
        assert store.writes[0][1]["parameters"] == {}
        assert json.loads(store.writes[0][1]["sidecar_fingerprint"]) == {}
        second = await scan(manager)
        assert second["skipped"] == 1 and len(store.writes) == 1
        path.with_suffix(".yaml").write_text(
            "tag: actual request\nmodel: nai-diffusion-4-5-full\ncfg: 0.5\nnegative: blur\ntoken: never-store\n",
            encoding="utf-8",
        )
        third = await scan(manager)
        assert third["skipped"] == 0 and len(store.writes) == 2
        assert store.writes[-1][1]["parameters"] == {
            "tag": "actual request",
            "model": "nai-diffusion-4-5-full",
            "cfg": 0.5,
            "negative": "blur",
        }
        fingerprint = json.loads(store.writes[-1][1]["sidecar_fingerprint"])
        assert fingerprint[path.with_suffix(".yaml").name]["size_bytes"] > 0
        path.with_suffix(".yaml").unlink()
        await scan(manager)
        assert len(store.writes) == 3
        assert store.writes[-1][1]["parameters"] == {}
        assert path.read_bytes() == b"original image bytes"
        path.unlink()
        report = await scan(manager)
        assert report["status"] == "complete" and report["indexed_count"] == 0
        assert store.reconciled[-1] == set()
        await manager.close()

    asyncio.run(run())


def test_current_yaml_wins_over_legacy_json_and_bad_yaml_retries(tmp_path):
    async def run():
        store, manager, root = manager_fixture(tmp_path)
        root.mkdir(parents=True)
        path = root / "nai_one.img"
        path.write_bytes(b"original")
        path.with_suffix(".json").write_text('{"tag":"legacy", "model":"old"}')
        await manager.configure({"nai": True})
        await scan(manager)
        assert store.writes[-1][1]["parameters"]["tag"] == "legacy"
        path.with_suffix(".yaml").write_text("tag: current\nmodel: new\n")
        await scan(manager)
        assert store.writes[-1][1]["parameters"]["tag"] == "current"
        before_reconcile = len(store.reconciled)
        path.with_suffix(".yaml").write_text("tag: &x [1]\nartist: *x\n")
        report = await scan(manager)
        assert report["status"] == "warning"
        assert "YAML" in report["errors"][0]
        assert len(store.reconciled) == before_reconcile
        assert store.writes[-1][1]["parameters"]["tag"] == "legacy"
        assert (
            json.loads(store.writes[-1][1]["sidecar_fingerprint"])["__parse_error__"]
            is True
        )
        write_count = len(store.writes)
        await scan(manager)
        assert len(store.writes) == write_count + 1
        await manager.close()

    asyncio.run(run())


def test_partial_enumeration_and_read_errors_preserve_existing_indices(
    tmp_path, monkeypatch
):
    async def run():
        store, manager, root = manager_fixture(tmp_path)
        root.mkdir(parents=True)
        store.images["nai_missing.png"] = {"size_bytes": 10}
        target = tmp_path / "outside.png"
        target.write_bytes(b"must not read")
        (root / "nai_link.png").symlink_to(target)
        (root / "nai_valid.png").write_bytes(b"valid")
        await manager.configure({"nai": True})
        report = await scan(manager)
        assert report["status"] == "warning"
        assert "nai_missing.png" in store.images
        assert "nai_link.png" not in store.images
        assert "nai_valid.png" in store.images
        assert store.reconciled == []
        (root / "nai_link.png").unlink()
        (root / "nai_valid.png").write_bytes(b"changed")
        original_reader = external_gallery._load_entry

        def denied(*args):
            raise PermissionError("read denied")

        monkeypatch.setattr(external_gallery, "_load_entry", denied)
        report = await scan(manager)
        assert report["status"] == "warning" and "read denied" in report["errors"][0]
        assert "nai_missing.png" in store.images and store.reconciled == []
        monkeypatch.setattr(external_gallery, "_load_entry", original_reader)
        report = await scan(manager)
        assert report["status"] == "complete" and "nai_missing.png" not in store.images
        await manager.close()

    asyncio.run(run())


def test_disable_invalidates_inflight_file_read_and_reenable_rebuilds(
    tmp_path, monkeypatch
):
    async def run():
        store, manager, root = manager_fixture(tmp_path)
        root.mkdir(parents=True)
        (root / "nai_one.png").write_bytes(b"original")
        reader_entered, release_reader = threading.Event(), threading.Event()
        actual_reader = external_gallery._load_entry

        def blocked(*args):
            reader_entered.set()
            assert release_reader.wait(5)
            return actual_reader(*args)

        monkeypatch.setattr(external_gallery, "_load_entry", blocked)
        try:
            await manager.configure({"nai": True})
            await manager.start()
            assert await asyncio.to_thread(reader_entered.wait, 5)
            await manager.configure({"nai": False})
            assert (await manager.status())[0]["status"] == "disabled"
            release_reader.set()
            assert not store.writes and not store.reconciled and not store.reports
            assert (root / "nai_one.png").exists()
            monkeypatch.setattr(external_gallery, "_load_entry", actual_reader)
            await manager.configure({"nai": True})
            await manager._tasks["nai"]
            assert len(store.writes) == 1
        finally:
            release_reader.set()
            await manager.close()

    asyncio.run(run())


def test_disable_waits_for_started_storage_mutation_to_settle(tmp_path, monkeypatch):
    async def run():
        store, manager, root = manager_fixture(tmp_path)
        root.mkdir(parents=True)
        (root / "nai_one.png").write_bytes(b"original")
        entered, release = asyncio.Event(), asyncio.Event()
        original_upsert = store.upsert_external_image

        async def delayed(*args, **kwargs):
            entered.set()
            await release.wait()
            return await original_upsert(*args, **kwargs)

        monkeypatch.setattr(store, "upsert_external_image", delayed)
        await manager.configure({"nai": True})
        await manager.start()
        await asyncio.wait_for(entered.wait(), 5)
        disabling = asyncio.create_task(manager.configure({"nai": False}))
        await asyncio.sleep(0)
        assert not disabling.done()
        release.set()
        await disabling
        assert store.sources["nai"]["enabled"] is False
        assert len(store.writes) == 1
        assert not store.reconciled and not store.reports
        assert (await manager.status())[0]["status"] == "disabled"
        await manager.close()

    asyncio.run(run())


def test_manual_requests_deduplicate_and_periodic_scan_discovers_new_files(tmp_path):
    async def run():
        store, manager, root = manager_fixture(tmp_path, scan_interval=0.01)
        root.mkdir(parents=True)
        await manager.configure({"nai": True})
        await manager.start()
        task = manager._tasks["nai"]
        await manager.request_scan("nai")
        assert manager._tasks["nai"] is task
        await task
        (root / "nai_new.webp").write_bytes(b"new")
        for _ in range(100):
            if store.images:
                break
            await asyncio.sleep(0.01)
        assert "nai_new.webp" in store.images
        await manager.close()
        assert manager._periodic_task is None
        assert not manager._tasks
        await manager.configure({"nai": False})
        with pytest.raises(ValueError, match="启用"):
            await manager.request_scan("nai")
        with pytest.raises(ValueError, match="未知"):
            await manager.request_scan("other")

    asyncio.run(run())


@pytest.mark.parametrize(
    "content,expected",
    [
        (
            b"tag: ordinary\nscale: 4\ncfg: 0.5\n",
            {"tag": "ordinary", "scale": 4, "cfg": 0.5},
        ),
        (b"tag: ['not', 'a prompt']\nscale: .inf\ntoken: secret\n", {}),
    ],
)
def test_nai_sidecar_whitelist_only_retains_primitive_request_values(content, expected):
    assert NAIGalleryAdapter().request_parameters(content, "nai_one.yaml") == expected


@pytest.mark.parametrize(
    "content",
    [
        "tag: &x hello\nartist: *x\n",
        "tag: " + "[" * 25 + "1" + "]" * 25,
        "tag: [" + ",".join("1" for _ in range(4100)) + "]",
        "!!python/object/apply:os.system ['false']",
    ],
)
def test_nai_sidecar_parser_rejects_aliases_deep_large_and_python_graphs(content):
    with pytest.raises((ValueError, external_gallery.yaml.YAMLError)):
        NAIGalleryAdapter().request_parameters(content.encode(), "nai_one.yaml")


def test_unreadable_source_is_error_not_an_empty_gallery(tmp_path, monkeypatch):
    async def run():
        store, manager, root = manager_fixture(tmp_path)
        root.mkdir(parents=True)
        store.images["nai_old.png"] = {"size_bytes": 7}

        def denied(*args):
            raise PermissionError("directory unreadable")

        monkeypatch.setattr(external_gallery, "_enumerate_source", denied)
        await manager.configure({"nai": True})
        report = await scan(manager)
        assert report["status"] == "error" and report["indexed_count"] == 1
        assert store.reconciled == []
        await manager.close()

    asyncio.run(run())


def test_source_adapter_can_add_another_plugin_without_nai_names_or_engine(tmp_path):
    class AnotherAdapter(ExternalGalleryAdapter):
        def accepts(self, filename):
            return filename.endswith(".png")

    async def run():
        store = MemoryStore()
        data_dir = tmp_path / "plugin_data" / "studio"
        adapter = AnotherAdapter(
            "another", "其他插件图库", "another_plugin", "outputs", "comfyui"
        )
        root = adapter.resolve_root(data_dir)
        root.mkdir(parents=True)
        (root / "output.png").write_bytes(b"source bytes")
        manager = ExternalGalleryManager(store, data_dir, adapters=[adapter])
        await manager.configure({"another": {"enabled": True}})
        await manager.start()
        await manager._tasks["another"]
        assert store.writes[0][1]["generation_engine"] == "comfyui"
        report = (await manager.status())[0]
        assert report["id"] == "another" and report["status"] == "complete"
        assert report["name"] == "其他插件图库"
        await manager.close()

    asyncio.run(run())


def test_symlink_source_root_is_not_followed(tmp_path):
    async def run():
        store, manager, root = manager_fixture(tmp_path)
        root.parent.mkdir(parents=True)
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "nai_image.png").write_bytes(b"outside")
        root.symlink_to(outside, target_is_directory=True)
        await manager.configure({"nai": True})
        report = await scan(manager)
        assert report["status"] == "unavailable"
        assert not store.writes and not store.reconciled
        await manager.close()

    asyncio.run(run())


def test_real_store_scanner_roundtrip_preserves_metadata_and_favorite_when_disabled(
    tmp_path,
):
    async def run():
        data_dir = tmp_path / "plugin_data" / "astrbot_plugin_image_studio"
        root = data_dir.parent / "astrbot_plugin_nai_image" / "image_history"
        root.mkdir(parents=True)
        original = root / "nai_1700000000000000000.png"
        metadata = PngImagePlugin.PngInfo()
        metadata.add_text("Software", "NovelAI")
        metadata.add_text(
            "Comment",
            json.dumps(
                {"prompt": "embedded prompt", "uc": "embedded negative", "seed": 23}
            ),
        )
        buffer = io.BytesIO()
        Image.new("RGB", (24, 32), "purple").save(buffer, "PNG", pnginfo=metadata)
        original.write_bytes(buffer.getvalue())
        original.with_suffix(".yaml").write_text(
            "tag: actual request\nnegative: actual negative\nmodel: nai-diffusion-4-5-full\ncfg: 0.5\n"
        )
        store = GenerationStore(data_dir)
        await store.initialize()
        manager = ExternalGalleryManager(store, data_dir)
        try:
            await manager.configure({"nai": True})
            report = await scan(manager)
            assert report["status"] == "complete", report
            assert report["indexed_count"] == 1
            assert report["thumbnail_count"] == 1
            gallery = await store.list_generations({})
            assert gallery["total"] == 1
            generation_id = gallery["items"][0]["id"]
            detail = await store.generation_detail(generation_id)
            assert detail["original_prompt"] == "actual request"
            assert (
                detail["images"][0]["metadata"]["normalized"]["prompt"]
                == "embedded prompt"
            )
            await store.set_favorite(generation_id, True)
            assert not list(store.assets_dir.iterdir())
            await manager.configure({"nai": False})
            assert (await store.list_generations({}))["total"] == 0
            assert original.read_bytes() == buffer.getvalue()
            assert (await manager.status())[0]["status"] == "disabled"
            await manager.configure({"nai": True})
            await scan(manager)
            detail = await store.generation_detail(generation_id)
            assert detail["is_favorite"] is True
            await manager.close()
            manager = ExternalGalleryManager(store, data_dir)
            await manager.configure({"nai": True})
            assert (await manager.status())[0]["status"] == "complete"
            original.unlink()
            await scan(manager)
            assert (await store.list_generations({}))["total"] == 0
        finally:
            await manager.close()
            await store.close()

    asyncio.run(run())


async def _real_scanned_source(tmp_path):
    data_dir = tmp_path / "plugin_data" / "astrbot_plugin_image_studio"
    root = data_dir.parent / "astrbot_plugin_nai_image" / "image_history"
    root.mkdir(parents=True)
    original = root / "nai_1700000000000000000.png"
    metadata = PngImagePlugin.PngInfo()
    metadata.add_text("Software", "NovelAI")
    metadata.add_text("Comment", json.dumps({"prompt": "embedded prompt", "seed": 23}))
    buffer = io.BytesIO()
    Image.new("RGB", (512, 768), "purple").save(buffer, "PNG", pnginfo=metadata)
    original.write_bytes(buffer.getvalue())
    store = GenerationStore(data_dir)
    await store.initialize()
    manager = ExternalGalleryManager(store, data_dir)
    await manager.configure({"nai": True})
    assert (await scan(manager))["status"] == "complete"
    return store, manager, original


def test_unchanged_external_images_refresh_preview_spec_and_parser_version(tmp_path):
    async def run():
        store, manager, original = await _real_scanned_source(tmp_path)
        try:
            assert (await scan(manager))["skipped"] == 1
            await manager.configure(
                {"nai": True}, preview_max_edge=256, preview_quality=55
            )
            assert (await scan(manager))["skipped"] == 0
            snapshot = (await store.external_scan_snapshot("nai"))[original.name]
            assert (
                snapshot["thumbnail_max_edge"] == 256
                and snapshot["thumbnail_quality"] == 55
            )
            with store._connect() as conn:
                row = conn.execute(
                    "SELECT metadata_json FROM image_metadata WHERE asset_id=?",
                    (snapshot["asset_id"],),
                ).fetchone()
                stale = json.loads(row[0])
                stale["parser_version"] = 0
                stale["normalized"]["prompt"] = "obsolete parser result"
                conn.execute(
                    "UPDATE image_metadata SET parser_version=0,metadata_json=? WHERE asset_id=?",
                    (json.dumps(stale), snapshot["asset_id"]),
                )
            assert (await scan(manager))["skipped"] == 0
            refreshed = (await store.external_scan_snapshot("nai"))[original.name]
            assert refreshed["parser_version"] == PARSER_VERSION
            detail = await store.generation_detail(refreshed["generation_id"])
            assert (
                detail["images"][0]["metadata"]["normalized"]["prompt"]
                == "embedded prompt"
            )
            assert (await scan(manager))["skipped"] == 1
        finally:
            await manager.close()
            await store.close()

    asyncio.run(run())


def test_deleting_last_local_duplicate_trims_disabled_external_thumbnail(tmp_path):
    async def run():
        store, manager, original = await _real_scanned_source(tmp_path)
        history = HistorySettings(True, 10, 10, True)
        provider = ImageProvider.from_mapping(
            {
                "id": "test",
                "kind": "openai_images",
                "base_url": "https://example.test",
                "model": "model",
            }
        )
        try:
            local_id = await store.record_success(
                provider=provider,
                request=GenerationRequest(
                    mode="text2img", provider_id="test", prompt="cat"
                ),
                images=(GeneratedImage(original.read_bytes(), "image/png"),),
                elapsed_ms=1,
                history=history,
            )
            await manager.configure({"nai": False})
            assert list(store.thumbnails_dir.glob("*.webp"))
            await store.delete_generation(local_id)
            assert original.exists()
            assert not list(store.assets_dir.rglob("*.png"))
            assert not list(store.thumbnails_dir.glob("*.webp"))
            await store.run_maintenance(
                history, preview_max_edge=768, preview_quality=80
            )
            assert not list(store.thumbnails_dir.glob("*.webp"))
            await manager.configure({"nai": True})
            assert (await scan(manager))["status"] == "complete"
            assert list(store.thumbnails_dir.glob("*.webp"))
        finally:
            await manager.close()
            await store.close()

    asyncio.run(run())


def test_malformed_sidecar_keeps_file_identity_for_explicit_deletion(tmp_path):
    async def run():
        store, manager, original = await _real_scanned_source(tmp_path)
        sidecar = original.with_suffix(".yaml")
        sidecar.write_text("tag: &x hello\nartist: *x\n")
        try:
            assert (await scan(manager))["status"] == "warning"
            snapshot = (await store.external_scan_snapshot("nai"))[original.name]
            identity = json.loads(snapshot["sidecar_fingerprint"])
            assert identity[sidecar.name]["size_bytes"] == sidecar.stat().st_size
            assert identity["__parse_error__"] is True
            assert await store.delete_generation(snapshot["generation_id"])
            assert not original.exists() and not sidecar.exists()
        finally:
            await manager.close()
            await store.close()

    asyncio.run(run())


def directory_setting(path, **changes):
    return {
        "type": "directory",
        "name": "普通图片",
        "path": str(path),
        "enabled": True,
        **changes,
    }


def test_custom_directory_reads_mixed_images_without_sidecars_and_recurses_when_enabled(
    tmp_path,
):
    async def run():
        store, manager, _ = manager_fixture(tmp_path)
        root = tmp_path / "photos"
        nested = root / "nested"
        nested.mkdir(parents=True)
        (root / "ordinary.jpeg").write_bytes(b"one")
        (root / "ordinary.yaml").write_text("tag: should not be read")
        (root / "animation.gif").write_bytes(b"two")
        (nested / "more.webp").write_bytes(b"three")
        await manager.configure({"photos": directory_setting(root)})
        await manager.request_scan("photos")
        await manager._tasks["photos"]
        assert set(store.images) == {"ordinary.jpeg", "animation.gif"}
        assert all(not item[1]["parameters"] for item in store.writes)
        assert all(
            not json.loads(item[1]["sidecar_fingerprint"]) for item in store.writes
        )
        assert all(item[1]["generation_engine"] == "unknown" for item in store.writes)
        await manager.configure({"photos": directory_setting(root, recursive=True)})
        await manager.request_scan("photos")
        await manager._tasks["photos"]
        assert "nested/more.webp" in store.images
        assert (await manager.status())[0]["status"] == "complete"
        await manager.close()

    asyncio.run(run())


def test_custom_missing_root_can_be_saved_and_does_not_purge(tmp_path):
    async def run():
        store, manager, _ = manager_fixture(tmp_path)
        root = tmp_path / "offline_mount"
        await manager.configure({"photos": directory_setting(root)})
        store.images["old.png"] = {"size_bytes": 20}
        await manager.request_scan("photos")
        await manager._tasks["photos"]
        assert (await manager.status())[0]["status"] == "unavailable"
        assert "old.png" in store.images and not store.reconciled
        assert not root.exists()
        await manager.close()

    asyncio.run(run())


def test_configuration_rejects_duplicates_overlaps_own_data_and_symlink_paths(tmp_path):
    _, manager, _ = manager_fixture(tmp_path)
    root = tmp_path / "photos"
    root.mkdir()
    child = root / "nested"
    for config in (
        {"a": directory_setting(root), "b": directory_setting(root, enabled=False)},
        {"a": directory_setting(root, recursive=True), "b": directory_setting(child)},
        {"a": directory_setting(manager.data_dir / "history" / "assets")},
        {"a": directory_setting(manager.data_dir.parent, recursive=True)},
        {"a": directory_setting("/")},
        {"a": directory_setting("relative")},
    ):
        with pytest.raises(ValueError):
            manager.validate_configuration(config)
    manager.validate_configuration(
        {"a": directory_setting(root), "b": directory_setting(child)}
    )
    manager.validate_configuration({"a": directory_setting(manager.data_dir.parent)})
    link = tmp_path / "link"
    link.symlink_to(root, target_is_directory=True)
    with pytest.raises((ValueError, NotADirectoryError), match="符号链接"):
        manager.validate_configuration({"a": directory_setting(link / "nested")})


def test_remove_source_cancels_inflight_scan_without_deleting_originals(
    tmp_path, monkeypatch
):
    async def run():
        store, manager, root = manager_fixture(tmp_path)
        root.mkdir(parents=True)
        original = root / "nai_one.png"
        original.write_bytes(b"one")
        entered, release = threading.Event(), threading.Event()
        reader = external_gallery._load_entry

        def blocked(*args):
            entered.set()
            assert release.wait(5)
            return reader(*args)

        monkeypatch.setattr(external_gallery, "_load_entry", blocked)
        try:
            await manager.configure({"nai": True})
            await manager.start()
            assert await asyncio.to_thread(entered.wait, 5)
            await manager.configure({})
            release.set()
            assert await manager.status() == []
            assert not store.sources and not store.writes and not manager._tasks
            assert original.exists()
            with pytest.raises(ValueError, match="未知"):
                await manager.request_scan("nai")
        finally:
            release.set()
            await manager.close()

    asyncio.run(run())


def test_restart_removes_stale_database_sources_and_has_no_fixed_nai_row(tmp_path):
    async def run():
        store, manager, _ = manager_fixture(tmp_path)
        store.sources["nai"] = {"id": "nai", "enabled": True}
        await manager.configure({})
        assert await manager.status() == [] and not store.sources
        assert {kind["id"] for kind in manager.source_types()} == {"nai", "directory"}
        await manager.close()

    asyncio.run(run())


def test_time_policy_upgrade_reindexes_once_but_name_and_permissions_do_not(tmp_path):
    async def run():
        store, manager, root = manager_fixture(tmp_path)
        root.mkdir(parents=True)
        original = root / "nai_1700000000000000000.png"
        original.write_bytes(b"one")
        await manager.configure({"nai": True})
        await scan(manager)
        store.images[original.name]["time_policy_version"] = 0
        assert (await scan(manager))["skipped"] == 0
        assert (await scan(manager))["skipped"] == 1
        writes = len(store.writes)
        await manager.configure(
            {
                "nai": {
                    "name": "新名称",
                    "enabled": True,
                    "permissions": {"delete": False},
                }
            }
        )
        assert (await scan(manager))["skipped"] == 1
        assert len(store.writes) == writes
        report = (await manager.status())[0]
        assert report["name"] == "新名称" and not report["permissions"]["delete"]
        await manager.close()

    asyncio.run(run())


def test_real_custom_gallery_metadata_free_image_has_no_fake_generation_parameters(
    tmp_path,
):
    async def run():
        data_dir = tmp_path / "plugin_data" / "astrbot_plugin_image_studio"
        root = tmp_path / "ordinary_photos"
        root.mkdir()
        output = io.BytesIO()
        Image.new("RGB", (32, 24), "green").save(output, "PNG")
        original = root / "ordinary.png"
        original.write_bytes(output.getvalue())
        original.with_suffix(".json").write_text('{"tag":"ignore", "model":"wrong"}')
        store = GenerationStore(data_dir)
        await store.initialize()
        manager = ExternalGalleryManager(store, data_dir)
        try:
            await manager.configure({"photos": directory_setting(root)})
            await manager.request_scan("photos")
            await manager._tasks["photos"]
            assert (await manager.status())[0]["status"] == "complete"
            gallery = await store.list_generations({})
            assert gallery["total"] == 1
            detail = await store.generation_detail(gallery["items"][0]["id"])
            assert not detail["original_prompt"]
            assert detail["generation_engine"] == "unknown"
            assert not detail.get("generated_at")
            assert not detail["parameters"].get("generated_at")
            assert original.exists()
        finally:
            await manager.close()
            await store.close()

    asyncio.run(run())
