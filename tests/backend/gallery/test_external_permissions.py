from __future__ import annotations

import asyncio
import json
import threading

import pytest
from astrbot_plugin_image_studio.backend.gallery.errors import ExternalPermissionError
from astrbot_plugin_image_studio.tests.backend.gallery.test_external_storage import (
    add,
    image_bytes,
    setup,
)


async def restrict(store, root, **permissions):
    return await store.configure_external_source(
        "nai", "NAI 插件图库", root, True, permissions=permissions
    )


def test_disabling_favorite_freezes_state_and_rejects_entire_mixed_selection(tmp_path):
    async def run():
        store, root = await setup(tmp_path)
        external, original = await add(store, root)
        local = await store.import_image(image_bytes("blue"), "local.png", {})
        await store.set_favorite(external["generation_id"], True)
        await restrict(store, root, favorite=False)
        preview = await store.external_action_preview(
            [local["generation_id"], external["generation_id"]], "favorite"
        )
        assert preview["allowed"] is False
        assert preview["denied"][0]["id"] == external["generation_id"]
        assert preview["denied"][0]["source_id"] == "nai"
        with pytest.raises(ExternalPermissionError, match="不允许修改收藏"):
            await store.set_favorite(external["generation_id"], False)
        # Even an already-favorite selection must not permit a partial batch.
        with pytest.raises(ExternalPermissionError):
            await store.toggle_favorites(
                [local["generation_id"], external["generation_id"]]
            )
        states = await store.favorite_status(
            [local["generation_id"], external["generation_id"]]
        )
        assert [item["is_favorite"] for item in states["items"]] == [False, True]
        assert original.is_file()
        await restrict(store, root, favorite=True)
        await store.set_favorite(external["generation_id"], False)
        await store.close()

    asyncio.run(run())


def test_download_permission_blocks_download_and_entire_export_but_not_view_or_reference(
    tmp_path,
):
    async def run():
        store, root = await setup(tmp_path)
        external, original = await add(store, root)
        local = await store.import_image(image_bytes("blue"), "local.png", {})
        await restrict(store, root, download=False)
        with pytest.raises(ExternalPermissionError, match="下载或导出原图"):
            await store.gallery_image_file(external["image_id"])
        with pytest.raises(ExternalPermissionError):
            await store.export_generations(
                [local["generation_id"], external["generation_id"]]
            )
        assert not list(store.exports_dir.iterdir())
        for detail in ("original", "preview"):
            media = await store.gallery_image_data(external["image_id"], detail=detail)
            assert media["data_url"].startswith("data:image/")
            assert media["allowed_actions"]["download"] is False
        assert (await store.generation_detail(external["generation_id"]))["images"][0][
            "data_url"
        ]
        reference = await store.stage_gallery_reference(external["image_id"])
        assert (await store.load_staged_references([reference["id"]]))[
            0
        ].data == original.read_bytes()
        local_detail = await store.generation_detail(local["generation_id"], light=True)
        assert await store.gallery_image_file(local_detail["images"][0]["id"])
        await store.close()

    asyncio.run(run())


def test_reference_permission_does_not_depend_on_download_permission(tmp_path):
    async def run():
        store, root = await setup(tmp_path)
        external, _ = await add(store, root)
        await restrict(store, root, reference=False, download=True)
        with pytest.raises(ExternalPermissionError, match="用作参考图"):
            await store.stage_gallery_reference(external["image_id"])
        assert not list(store.staging_dir.iterdir())
        assert await store.gallery_image_file(external["image_id"])
        await store.close()

    asyncio.run(run())


@pytest.mark.parametrize("operation", ["generation", "images", "batch"])
def test_delete_permissions_checked_before_touching_any_selected_files(
    tmp_path, operation
):
    async def run():
        store, root = await setup(tmp_path)
        external, original = await add(store, root)
        local = await store.import_image(image_bytes("blue"), "local.png", {})
        await restrict(store, root, delete=False)
        with pytest.raises(ExternalPermissionError, match="删除原图"):
            if operation == "generation":
                await store.delete_generation(external["generation_id"])
            elif operation == "images":
                await store.delete_images(
                    external["generation_id"], [external["image_id"]]
                )
            else:
                await store.delete_generations(
                    [local["generation_id"], external["generation_id"]]
                )
        assert (await store.list_generations({}))["total"] == 2
        assert original.exists()
        local_detail = await store.generation_detail(local["generation_id"], light=True)
        assert await store.gallery_image_file(local_detail["images"][0]["id"])
        await store.close()

    asyncio.run(run())


def test_allowed_actions_present_on_all_gallery_media_shapes(tmp_path):
    async def run():
        store, root = await setup(tmp_path)
        external, _ = await add(store, root)
        expected = {
            "favorite": False,
            "delete": False,
            "download": True,
            "reference": False,
        }
        await restrict(store, root, **expected)
        listing = await store.list_generations({})
        manifest = await store.generation_detail(external["generation_id"], light=True)
        detail = await store.generation_detail(
            external["generation_id"], include_assets=False
        )
        info = await store.gallery_image_info(external["image_id"])
        sequence = await store.gallery_image_sequence({})
        media = await store.gallery_image_data(external["image_id"], detail="preview")
        for item in (
            listing["items"][0],
            manifest,
            manifest["images"][0],
            detail,
            detail["images"][0],
            info["image"],
            info["detail_fields"],
            sequence[0],
            media,
        ):
            assert item["allowed_actions"] == expected
        await store.close()

    asyncio.run(run())


def test_custom_directory_does_not_delete_sidecars_even_if_index_contains_them(
    tmp_path,
):
    async def run():
        store, root = await setup(tmp_path)
        await store.configure_external_source(
            "nai",
            "自定义目录",
            root,
            True,
            source_type="directory",
            permissions={"delete": True},
        )
        sidecar = root / "nai_1.json"
        sidecar.write_text(json.dumps({"notes": "keep this unrelated file"}))
        external, original = await add(store, root, sidecars={sidecar.name: {}})
        assert await store.delete_generation(external["generation_id"])
        assert not original.exists() and sidecar.exists()
        await store.close()

    asyncio.run(run())


def test_cancelled_favorite_keeps_source_permission_change_ordered(
    tmp_path, monkeypatch
):
    async def run():
        store, root = await setup(tmp_path)
        external, _ = await add(store, root)
        started, release = threading.Event(), threading.Event()
        set_favorite = store.records.set_favorite

        def paused(*args):
            started.set()
            assert release.wait(5)
            return set_favorite(*args)

        monkeypatch.setattr(store.records, "set_favorite", paused)
        operation = asyncio.create_task(
            store.set_favorite(external["generation_id"], True)
        )
        assert await asyncio.to_thread(started.wait, 5)
        operation.cancel()
        configure = asyncio.create_task(restrict(store, root, favorite=False))
        await asyncio.sleep(0)
        assert not configure.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await operation
        await configure
        assert (await store.generation_detail(external["generation_id"], light=True))[
            "is_favorite"
        ]
        with pytest.raises(ExternalPermissionError):
            await store.set_favorite(external["generation_id"], False)
        await store.close()

    asyncio.run(run())
