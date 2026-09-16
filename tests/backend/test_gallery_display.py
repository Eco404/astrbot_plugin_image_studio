"""Display-image size, cache isolation, permissions and nonblocking encodes."""

from __future__ import annotations

import asyncio
import base64
import io
import os
import threading
from pathlib import Path

import httpx
import pytest
from PIL import Image, PngImagePlugin

from astrbot_plugin_image_studio.backend.gallery.store import GenerationStore
from astrbot_plugin_image_studio.backend.media import display as media
from astrbot_plugin_image_studio.tests.backend.test_external_storage import add, setup
from astrbot_plugin_image_studio.tests.support.webui_harness import create_app


def picture(*, size=(2736, 1536), color=(180, 120, 90, 127), orientation=1):
    output = io.BytesIO()
    image = Image.new("RGBA", size, color)
    exif = Image.Exif()
    exif[274] = orientation
    exif[270] = "private-exif-description"
    pnginfo = PngImagePlugin.PngInfo()
    pnginfo.add_text("Comment", "private-prompt-and-workflow")
    image.save(output, "PNG", pnginfo=pnginfo, exif=exif)
    return output.getvalue()


async def imported(store, data=None):
    result = await store.import_image(data or picture(), "display.png", {})
    detail = await store.generation_detail(result["generation_id"], light=True)
    return detail["images"][0]["id"]


def decoded(payload):
    raw = base64.b64decode(payload["data_url"].split(",", 1)[1])
    return raw, Image.open(io.BytesIO(raw))


@pytest.mark.parametrize(
    "edge,expected", [(1, 768), (768, 768), (800, 1024), (1200, 1536), (2048, 2048)]
)
def test_display_sizes_are_bucketed_and_keep_original_geometry(
    tmp_path, edge, expected
):
    async def run():
        store = GenerationStore(tmp_path)
        await store.initialize()
        try:
            source = picture()
            image_id = await imported(store, source)
            path = (await store.gallery_image_file(image_id))[0]
            previous = path.stat()
            payload = await store.gallery_image_data(
                image_id, detail="display", max_edge=edge
            )
            raw, image = decoded(payload)
            assert payload["mime_type"] == "image/webp" and image.format == "WEBP"
            assert payload["max_edge"] == expected
            assert payload["width"] == 2736 and payload["height"] == 1536
            assert image.size == (payload["display_width"], payload["display_height"])
            assert max(image.size) == expected
            assert payload["size_bytes"] == len(raw)
            assert image.mode == "RGBA" and image.getpixel((0, 0))[3] == 127
            assert (
                "exif" not in image.info
                and "xmp" not in image.info
                and "icc_profile" not in image.info
            )
            assert b"private-" not in raw
            assert (
                path.read_bytes() == source
                and path.stat().st_mtime_ns == previous.st_mtime_ns
            )
            original = await store.gallery_image_data(image_id, detail="original")
            assert (
                decoded(original)[0] == source and original["mime_type"] == "image/png"
            )
            assert "display_width" not in original and "max_edge" not in original
            preview = await store.gallery_image_data(image_id, detail="preview")
            assert "display_width" not in preview and "max_edge" not in preview
        finally:
            await store.close()

    asyncio.run(run())


def test_display_applies_exif_orientation_and_never_upscales(tmp_path):
    async def run():
        store = GenerationStore(tmp_path)
        await store.initialize()
        try:
            image_id = await imported(store, picture(size=(120, 80), orientation=6))
            payload = await store.gallery_image_data(image_id, detail="display")
            assert (payload["width"], payload["height"]) == (80, 120)
            assert (payload["display_width"], payload["display_height"]) == (80, 120)
            assert (
                payload["max_edge"] == 1536
                and decoded(payload)[1].getexif().get(274) is None
            )
        finally:
            await store.close()

    asyncio.run(run())


def test_display_cache_reuses_encoding_and_rechecks_local_file_version(
    tmp_path, monkeypatch
):
    async def run():
        store = GenerationStore(tmp_path)
        await store.initialize()
        try:
            image_id = await imported(store)
            path = (await store.gallery_image_file(image_id))[0]
            encode = media.encode_display_image
            calls = []

            def counted(reader, max_edge):
                calls.append(max_edge)
                return encode(reader, max_edge)

            monkeypatch.setattr(media, "encode_display_image", counted)
            first = await store.gallery_image_data(
                image_id, detail="display", max_edge=800
            )
            second = await store.gallery_image_data(
                image_id, detail="display", max_edge=1024
            )
            assert first == second and calls == [1024]
            await store.gallery_image_data(image_id, detail="display", max_edge=2048)
            assert calls == [1024, 2048]
            # Even same-path, same-size, same-timestamp contents are invalidated
            # by ctime/inode in the key; no gallery database mutation is required.
            old_version = path.stat()
            changed = picture(color=(40, 80, 220, 127))
            path.write_bytes(changed)
            os.utime(path, ns=(old_version.st_atime_ns, old_version.st_mtime_ns))
            fresh = await store.gallery_image_data(
                image_id, detail="display", max_edge=1024
            )
            assert fresh["data_url"] != first["data_url"] and calls == [
                1024,
                2048,
                1024,
            ]
            path.unlink()
            assert (
                await store.gallery_image_data(
                    image_id, detail="display", max_edge=1024
                )
                is None
            )
        finally:
            await store.close()

    asyncio.run(run())


@pytest.mark.parametrize(
    "failure", ["disabled", "missing", "changed", "permission", "unreadable"]
)
def test_warm_external_display_cache_cannot_bypass_availability(
    tmp_path, monkeypatch, failure
):
    async def run():
        store, root = await setup(tmp_path)
        try:
            result, path = await add(store, root)
            await store.configure_external_source(
                "nai",
                "NAI",
                root,
                True,
                permissions={"download": False, "reference": False},
            )
            payload = await store.gallery_image_data(
                result["image_id"], detail="display"
            )
            assert payload and payload["allowed_actions"]["download"] is False
            assert payload["allowed_actions"]["reference"] is False
            reports = []
            store.set_external_issue_handler(
                lambda source, reason: reports.append((source, reason))
            )
            if failure == "disabled":
                await store.configure_external_source("nai", "NAI", root, False)
            elif failure == "missing":
                path.unlink()
            elif failure == "changed":
                path.write_bytes(picture(size=(32, 48)))
            elif failure == "unreadable":
                original_open = Path.open

                def denied_file(candidate, *args, **kwargs):
                    if candidate == path:
                        raise PermissionError("denied")
                    return original_open(candidate, *args, **kwargs)

                monkeypatch.setattr(Path, "open", denied_file)
            else:

                def denied(*args, **kwargs):
                    raise PermissionError("denied")

                monkeypatch.setattr(store.external_records, "external_path", denied)
            assert (
                await store.gallery_image_data(result["image_id"], detail="display")
                is None
            )
            assert reports == (
                [("nai", failure)] if failure in {"missing", "changed"} else []
            )
        finally:
            await store.close()

    asyncio.run(run())


def test_source_disabled_during_encode_cannot_return_display_bytes(
    tmp_path, monkeypatch
):
    async def run():
        store, root = await setup(tmp_path)
        release = threading.Event()
        started = threading.Event()
        encode = media.encode_display_image
        task = None

        def held(reader, max_edge):
            result = encode(reader, max_edge)
            started.set()
            assert release.wait(5)
            return result

        try:
            result, _ = await add(store, root)
            monkeypatch.setattr(media, "encode_display_image", held)
            task = asyncio.create_task(
                store.gallery_image_data(result["image_id"], detail="display")
            )
            assert await asyncio.to_thread(started.wait, 3)
            await store.configure_external_source("nai", "NAI", root, False)
            release.set()
            assert await task is None
        finally:
            release.set()
            if task is not None:
                await asyncio.gather(task, return_exceptions=True)
            await store.close()

    asyncio.run(run())


def test_display_encodes_coalesce_and_do_not_block_gallery_queries(
    tmp_path, monkeypatch
):
    async def run():
        store = GenerationStore(tmp_path)
        await store.initialize()
        release = threading.Event()
        two_started = threading.Event()
        guard = threading.Lock()
        active = 0
        peak = 0
        calls = 0
        encode = media.encode_display_image

        def held(reader, max_edge):
            nonlocal active, peak, calls
            with guard:
                active += 1
                calls += 1
                peak = max(peak, active)
                if active == 2:
                    two_started.set()
            try:
                assert release.wait(5), "test did not release workers"
                return encode(reader, max_edge)
            finally:
                with guard:
                    active -= 1

        tasks = []
        try:
            ids = [
                await imported(store, picture(size=(64, 64), color=color))
                for color in ("red", "green", "blue")
            ]
            monkeypatch.setattr(media, "encode_display_image", held)
            tasks = [
                asyncio.create_task(
                    store.gallery_image_data(image_id, detail="display")
                )
                for image_id in [ids[0], ids[0], ids[1], ids[2]]
            ]
            assert await asyncio.to_thread(two_started.wait, 3)
            listing = await asyncio.wait_for(store.list_generations({"light": True}), 1)
            assert listing["total"] == 3 and calls == 2
            # Cancelling a waiter must not cancel a shared active encode.
            tasks[0].cancel()
            with pytest.raises(asyncio.CancelledError):
                await tasks[0]
            release.set()
            results = await asyncio.gather(*tasks[1:])
            assert all(result["data_url"] for result in results)
            assert peak == 2 and calls == 3
        finally:
            release.set()
            await asyncio.gather(*tasks, return_exceptions=True)
            await store.close()

    asyncio.run(run())


def test_display_cache_respects_encoded_byte_and_item_budgets(monkeypatch):
    async def run():
        def encode(reader, max_edge):
            return media.DisplayImage(reader(), 1, 1)

        monkeypatch.setattr(media, "encode_display_image", encode)
        cache = media.DisplayImageCache(max_bytes=7, max_items=2)
        await cache.get("a", lambda: b"aaa", 768)
        await cache.get("b", lambda: b"bbb", 768)
        await cache.get("a", lambda: pytest.fail("a must still be cached"), 768)
        await cache.get("c", lambda: b"cccc", 768)
        assert list(cache._entries) == ["a", "c"] and cache.size_bytes == 7
        await cache.get("large", lambda: b"12345678", 768)
        assert list(cache._entries) == ["a", "c"] and cache.size_bytes == 7
        await cache.get("d", lambda: b"d", 768)
        assert list(cache._entries) == ["c", "d"] and cache.size_bytes == 5
        await cache.close()
        assert cache.size_bytes == 0 and not cache._entries

    asyncio.run(run())


def test_display_api_validates_size_and_never_falls_back_to_original(tmp_path):
    async def run():
        app = await create_app(tmp_path, seed=False)
        store = app.state.plugin.store
        try:
            image_id = await imported(store, picture(size=(120, 80)))
            endpoint = f"/astrbot_plugin_image_studio/gallery/image/{image_id}"
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                for edge in (
                    "",
                    "0",
                    "-1",
                    "2049",
                    "9999999999999",
                    "nan",
                    "1.5",
                    "true",
                    "[]",
                ):
                    response = await client.get(
                        endpoint, params={"detail": "display", "max_edge": edge}
                    )
                    assert response.status_code == 400, edge
                for edge in (False, None, 1536.0, []):
                    with pytest.raises(ValueError, match="max_edge"):
                        await store.gallery_image_data(
                            image_id, detail="display", max_edge=edge
                        )
                response = await client.get(endpoint, params={"detail": "display"})
                assert (
                    response.status_code == 200 and response.json()["max_edge"] == 1536
                )
                path = (await store.gallery_image_file(image_id))[0]
                path.write_bytes(b"not-an-image-private-metadata")
                response = await client.get(endpoint, params={"detail": "display"})
                assert response.status_code == 422 and "data_url" not in response.json()
                assert "private-metadata" not in response.text
                response = await client.get(endpoint, params={"detail": "bogus"})
                assert response.status_code == 400
        finally:
            await store.close()

    asyncio.run(run())
