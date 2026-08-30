from __future__ import annotations

import asyncio
import base64
import json
import re
import sqlite3
import zipfile

from astrbot_plugin_image_gen.config import HistorySettings
from astrbot_plugin_image_gen.models import (
    GeneratedImage,
    GenerationRequest,
    ImageProvider,
    ReferenceImage,
)
from astrbot_plugin_image_gen.storage import GenerationStore

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Y9JZq4AAAAASUVORK5CYII="
)


def provider() -> ImageProvider:
    return ImageProvider.from_mapping(
        {
            "id": "test-provider",
            "name": "Test Provider",
            "kind": "openai_images",
            "base_url": "https://example.test",
            "model": "test-image",
            "supports_text2img": True,
            "supports_img2img": True,
            "max_reference_images": 1,
        }
    )


def test_gallery_keeps_references_out_of_collection_and_deletes_them(tmp_path) -> None:
    async def run() -> None:
        store = GenerationStore(tmp_path)
        await store.initialize()
        reference = ReferenceImage("source-ref", "source.png", PNG, "image/png")
        request = GenerationRequest(
            mode="img2img",
            provider_id="test-provider",
            prompt="blue circle",
            references=(reference,),
        )
        generation_id = await store.record_success(
            provider=provider(),
            request=request,
            images=(GeneratedImage(PNG, "image/png"),),
            elapsed_ms=42,
            history=HistorySettings(True, 10, 50, True),
        )
        thumbnail_paths = list(store.thumbnails_dir.glob("*.webp"))
        assert len(thumbnail_paths) == 1

        listing = await store.list_generations({})
        assert [item["id"] for item in listing["items"]] == [generation_id]
        assert "references" not in listing["items"][0]

        detail = await store.generation_detail(generation_id)
        assert detail is not None
        reference_id = detail["references"][0]["id"]
        assert detail["references"][0]["available"] is True
        assert await store.delete_reference(reference_id) is True

        detail = await store.generation_detail(generation_id)
        assert detail is not None
        assert detail["references"][0]["available"] is False
        assert detail["images"][0]["data_url"].startswith("data:image/png;base64,")
        assert await store.delete_generation(generation_id) is True
        assert not thumbnail_paths[0].exists()

    asyncio.run(run())


def test_initialize_removes_only_orphaned_asset_files(tmp_path) -> None:
    async def run() -> None:
        store = GenerationStore(tmp_path)
        await store.initialize()
        await store.record_success(
            provider=provider(),
            request=GenerationRequest(
                mode="text2img",
                provider_id="test-provider",
                prompt="keep thumbnail",
            ),
            images=(GeneratedImage(PNG, "image/png"),),
            elapsed_ms=12,
            history=HistorySettings(True, 10, 50, False),
        )
        referenced = next(store.thumbnails_dir.glob("*.webp"))
        referenced_asset = next(
            path for path in store.assets_dir.rglob("*") if path.is_file()
        )
        orphan_thumbnail = store.thumbnails_dir / "orphan.webp"
        orphan_thumbnail.write_bytes(PNG)
        orphan_asset = store.assets_dir / "orphan.png"
        orphan_asset.write_bytes(PNG)

        await store.initialize()

        assert referenced.is_file()
        assert referenced_asset.is_file()
        assert not orphan_thumbnail.exists()
        assert not orphan_asset.exists()

    asyncio.run(run())


def test_identical_images_share_one_content_addressed_asset(tmp_path) -> None:
    async def run() -> None:
        store = GenerationStore(tmp_path)
        await store.initialize()
        reference = ReferenceImage("same-ref", "same.png", PNG, "image/png")
        generation_ids = []
        for prompt in ("first", "second"):
            generation_ids.append(
                await store.record_success(
                    provider=provider(),
                    request=GenerationRequest(
                        mode="img2img",
                        provider_id="test-provider",
                        prompt=prompt,
                        references=(reference,),
                    ),
                    images=(GeneratedImage(PNG, "image/png"),),
                    elapsed_ms=12,
                    history=HistorySettings(True, 10, 50, True),
                )
            )

        asset_files = [path for path in store.assets_dir.rglob("*") if path.is_file()]
        thumbnail_files = list(store.thumbnails_dir.glob("*.webp"))
        assert len(asset_files) == 1
        assert len(thumbnail_files) == 1
        with sqlite3.connect(store.db_path) as conn:
            assert conn.execute("SELECT COUNT(*) FROM image_assets").fetchone()[0] == 1
            asset_ids = {
                row[0]
                for row in conn.execute(
                    "SELECT asset_id FROM generation_images "
                    "UNION SELECT asset_id FROM generation_references"
                )
            }
        assert len(asset_ids) == 1

        assert await store.delete_generation(generation_ids[0]) is True
        assert asset_files[0].is_file()
        assert thumbnail_files[0].is_file()

        second_detail = await store.generation_detail(generation_ids[1])
        assert second_detail is not None
        assert await store.delete_reference(second_detail["references"][0]["id"])
        assert asset_files[0].is_file()
        assert await store.delete_generation(generation_ids[1]) is True
        assert not asset_files[0].exists()
        assert not thumbnail_files[0].exists()

    asyncio.run(run())


def test_gallery_export_is_a_valid_zip_archive(tmp_path) -> None:
    async def run() -> None:
        store = GenerationStore(tmp_path)
        await store.initialize()
        generation_id = await store.record_success(
            provider=provider(),
            request=GenerationRequest(
                mode="text2img",
                provider_id="test-provider",
                prompt="export check",
            ),
            images=(GeneratedImage(PNG, "image/png"),),
            elapsed_ms=12,
            history=HistorySettings(True, 10, 50, False),
        )

        archive_path = await store.export_generations([generation_id])
        assert archive_path.stat().st_size > 100
        with zipfile.ZipFile(archive_path) as archive:
            assert archive.testzip() is None
            names = archive.namelist()
            assert len(names) == 2
            assert all("/" not in name for name in names)
            image_name = next(name for name in names if not name.endswith(".json"))
            assert re.fullmatch(r"\d{14}_t2i_test-image\.png", image_name)
            json_name = image_name.removesuffix(".png") + ".json"
            assert json_name in names
            metadata = json.loads(archive.read(json_name))
            assert metadata["id"] == generation_id
            assert metadata["model"] == "test-image"
            assert metadata["image"]["filename"] == image_name
            assert "path" not in metadata["image"]

        summary = await store.generation_detail(generation_id, include_assets=False)
        assert summary is not None
        assert summary["images"][0]["data_url"] == ""
        assert summary["images"][0]["path"]

    asyncio.run(run())


def test_gallery_export_pairs_each_image_in_flat_archive(tmp_path) -> None:
    async def run() -> None:
        store = GenerationStore(tmp_path)
        await store.initialize()
        generation_id = await store.record_success(
            provider=provider(),
            request=GenerationRequest(
                mode="img2img",
                provider_id="test-provider",
                prompt="export two images",
            ),
            images=(
                GeneratedImage(PNG, "image/png"),
                GeneratedImage(PNG, "image/png"),
            ),
            elapsed_ms=12,
            history=HistorySettings(True, 10, 50, False),
        )

        archive_path = await store.export_generations([generation_id])
        with zipfile.ZipFile(archive_path) as archive:
            names = archive.namelist()
            assert len(names) == 4
            assert all("/" not in name for name in names)
            image_names = sorted(name for name in names if name.endswith(".png"))
            assert all(
                re.fullmatch(r"\d{14}_i2i_test-image_0[12]\.png", name)
                for name in image_names
            )
            for image_name in image_names:
                assert image_name.removesuffix(".png") + ".json" in names

    asyncio.run(run())
