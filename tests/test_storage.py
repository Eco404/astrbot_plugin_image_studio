from __future__ import annotations

import asyncio
import base64
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
            assert "manifest.json" in archive.namelist()
            assert any(
                name.startswith(f"images/{generation_id}/")
                for name in archive.namelist()
            )

        summary = await store.generation_detail(generation_id, include_assets=False)
        assert summary is not None
        assert summary["images"][0]["data_url"] == ""
        assert summary["images"][0]["path"]

    asyncio.run(run())
