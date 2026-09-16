from __future__ import annotations

import asyncio
import hashlib

import httpx
import pytest

from astrbot_plugin_image_studio.tests.backend.test_gallery_api import PREFIX
from astrbot_plugin_image_studio.tests.backend.test_import_groups import image
from astrbot_plugin_image_studio.tests.support.webui_harness import (
    create_app,
    fixture_image,
)


async def target_record(app, *, color="red", engine="novelai", model="target-model"):
    result = await app.state.plugin.store.import_image(
        image(color, model=model),
        "target.png",
        {"generation_engine": engine, "model": model},
    )
    return result["generation_id"]


async def prepare_merge(
    client, target_id, *, count=1, engine="novelai", overrides=None, data=None
):
    images = data or [image(color) for color in ("blue", "green")[:count]]
    response = await client.post(
        PREFIX + "imports/prepare",
        json={
            "merge_target_id": target_id,
            "generation_engine": engine,
            "items": [
                {
                    "client_id": f"merge-item-{index}",
                    "filename": f"merge-{index}.png",
                    "sha256": hashlib.sha256(images[index]).hexdigest(),
                    "overrides": overrides or {},
                }
                for index in range(count)
            ],
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


async def upload(client, prepared, data, index=0):
    return await client.post(
        PREFIX + prepared["items"][index]["upload_endpoint"],
        files={"file": ("image.png", data, "image/png")},
    )


def test_merge_targets_are_paginated_import_only_and_include_single_images(tmp_path):
    async def run():
        app = await create_app(tmp_path)
        target = await target_record(app)
        other = await target_record(app, color="blue", model="other-model")
        await target_record(app, color="green", engine="unknown")
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.get(
                PREFIX + "imports/merge-targets",
                params={"generation_engine": "NAI", "limit": 1},
            )
            assert response.status_code == 200, response.text
            first = response.json()
            assert first["total"] == 2 and len(first["items"]) == 1
            next_page = (
                await client.get(
                    PREFIX + "imports/merge-targets",
                    params={"generation_engine": "novelai", "limit": 1, "offset": 1},
                )
            ).json()
            assert {first["items"][0]["id"], next_page["items"][0]["id"]} == {
                target,
                other,
            }
            assert first["items"][0]["image_count"] == 1
            assert "images" not in first["items"][0]
            empty = await client.get(
                PREFIX + "imports/merge-targets",
                params={"generation_engine": "comfyui"},
            )
            assert empty.status_code == 200 and empty.json()["total"] == 0
            filtered = await client.get(
                PREFIX + "imports/merge-targets",
                params={"generation_engine": "novelai", "query": "other-model"},
            )
            assert filtered.status_code == 200 and filtered.json()["total"] == 1

    asyncio.run(run())


def test_merge_single_into_single_and_retry_retains_individual_models(tmp_path):
    async def run():
        app = await create_app(tmp_path, seed=False)
        target = await target_record(app)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            prepared = await prepare_merge(
                client, target, engine="nai", data=[image("blue", model="new-model")]
            )
            staged = await upload(client, prepared, image("blue", model="new-model"))
            assert staged.status_code == 200
            group = app.state.plugin._import_api().groups[prepared["group_id"]]
            response = await client.post(PREFIX + prepared["commit_endpoint"], json={})
            assert response.status_code == 200, response.text
            result = response.json()
            assert result["merged"] and result["added_count"] == 1
            assert result["generation_id"] == target
            assert result["image_count"] == 2
            assert not group["directory"].exists()
            assert (
                await client.post(PREFIX + prepared["commit_endpoint"], json={})
            ).json() == result
            detail = (
                await client.get(
                    PREFIX + f"gallery/detail/{target}", params={"assets": 0}
                )
            ).json()
            assert [entry["supplemental"]["model"] for entry in detail["images"]] == [
                "target-model",
                "new-model",
            ]
            assert (await client.get(PREFIX + "gallery/list")).json()["total"] == 1
            assert not any(key in result for key in ("import_key", "path", "directory"))

    asyncio.run(run())


@pytest.mark.parametrize(
    "extra",
    [
        {"as_group": True},
        {"generation_engine": "unknown"},
        {"generation_engine": "mixed"},
        {"merge_target_id": None},
        {"merge_target_id": "../../outside"},
    ],
)
def test_merge_prepare_rejects_invalid_target_or_ambiguous_source(tmp_path, extra):
    async def run():
        app = await create_app(tmp_path, seed=False)
        target = await target_record(app)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.post(
                PREFIX + "imports/prepare",
                json={
                    "merge_target_id": target,
                    "generation_engine": "novelai",
                    "items": [{"client_id": "a", "sha256": "a" * 64}],
                    **extra,
                },
            )
            assert response.status_code == 400
            assert app.state.plugin._import_api().uploads == {}
            assert app.state.plugin._import_api().groups == {}

    asyncio.run(run())


def test_merge_declared_source_mismatch_rejected_before_upload(tmp_path):
    async def run():
        app = await create_app(tmp_path, seed=False)
        target = await target_record(app)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.post(
                PREFIX + "imports/prepare",
                json={
                    "merge_target_id": target,
                    "generation_engine": "novelai",
                    "items": [
                        {
                            "client_id": "a",
                            "filename": "one.png",
                            "sha256": "a" * 64,
                            "overrides": {"generation_engine": "novelai"},
                        },
                        {
                            "client_id": "b",
                            "filename": "two.png",
                            "sha256": "b" * 64,
                            "overrides": {"generation_engine": "comfyui"},
                        },
                    ],
                },
            )
            assert response.status_code == 400
            assert "two.png" in response.json()["message"]
            assert not app.state.plugin._import_api().uploads

    asyncio.run(run())


def test_merge_cancel_discards_staging_without_touching_target(tmp_path):
    async def run():
        app = await create_app(tmp_path, seed=False)
        target = await target_record(app)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            prepared = await prepare_merge(client, target)
            await upload(client, prepared, image("blue"))
            group = app.state.plugin._import_api().groups[prepared["group_id"]]
            cancelled = await client.post(PREFIX + prepared["cancel_endpoint"], json={})
            assert cancelled.status_code == 200
            assert not group["directory"].exists()
            assert (
                await client.post(PREFIX + prepared["commit_endpoint"], json={})
            ).status_code == 410
            detail = await app.state.plugin.store.generation_detail(
                target, include_assets=False
            )
            assert len(detail["images"]) == 1

    asyncio.run(run())


def test_merge_commit_source_error_discards_invalid_batch_for_new_prepare(tmp_path):
    async def run():
        app = await create_app(tmp_path, seed=False)
        target = await target_record(app)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            prepared = await prepare_merge(client, target, count=2)
            await upload(client, prepared, image("blue"))
            incomplete = await client.post(
                PREFIX + prepared["commit_endpoint"], json={}
            )
            assert (
                incomplete.status_code == 400
                and "未上传" in incomplete.json()["message"]
            )
            await upload(client, prepared, image("green"), 1)
            group = app.state.plugin._import_api().groups[prepared["group_id"]]
            group["items"][1][1]["overrides"]["generation_engine"] = "comfyui"
            failed = await client.post(PREFIX + prepared["commit_endpoint"], json={})
            assert failed.status_code == 400
            assert not group["directory"].exists()
            detail = await app.state.plugin.store.generation_detail(
                target, include_assets=False
            )
            assert len(detail["images"]) == 1
            replacement = await prepare_merge(client, target, count=2)
            await upload(client, replacement, image("blue"))
            await upload(client, replacement, image("green"), 1)
            retried = await client.post(
                PREFIX + replacement["commit_endpoint"], json={}
            )
            assert retried.status_code == 200, retried.text
            assert retried.json()["added_count"] == 2

    asyncio.run(run())


def test_merge_commit_rechecks_deleted_target_and_does_not_create_replacement(tmp_path):
    async def run():
        app = await create_app(tmp_path, seed=False)
        target = await target_record(app)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            prepared = await prepare_merge(client, target)
            await upload(client, prepared, image("blue"))
            await app.state.plugin.store.delete_generation(target)
            response = await client.post(PREFIX + prepared["commit_endpoint"], json={})
            assert response.status_code == 400
            assert "目标" in response.json()["message"]
            assert (await client.get(PREFIX + "gallery/list")).json()["total"] == 0
            assert prepared["group_id"] not in app.state.plugin._import_api().groups

    asyncio.run(run())


def test_actual_uploaded_unknown_source_fails_then_reupload_can_retry(tmp_path):
    async def run():
        app = await create_app(tmp_path, seed=False)
        target = await target_record(app)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            prepared = await prepare_merge(client, target, data=[fixture_image(33)])
            assert (
                await upload(client, prepared, fixture_image(33))
            ).status_code == 200
            failed = await client.post(PREFIX + prepared["commit_endpoint"], json={})
            assert failed.status_code == 400
            assert "来源" in failed.json()["message"]
            prepared = await prepare_merge(
                client, target, data=[image("blue", model="reuploaded-model")]
            )
            assert (
                await upload(client, prepared, image("blue", model="reuploaded-model"))
            ).status_code == 200
            success = await client.post(PREFIX + prepared["commit_endpoint"], json={})
            assert success.status_code == 200, success.text
            assert success.json()["image_count"] == 2

    asyncio.run(run())


def test_merge_target_source_mutation_is_rechecked_at_commit(tmp_path):
    async def run():
        app = await create_app(tmp_path, seed=False)
        target = await target_record(app)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            prepared = await prepare_merge(client, target)
            await upload(client, prepared, image("blue"))
            with app.state.plugin.store._connect() as conn:
                conn.execute(
                    "UPDATE generation_images SET supplemental_json = json_set(supplemental_json, '$.generation_engine', 'comfyui') WHERE generation_id = ?",
                    (target,),
                )
            response = await client.post(PREFIX + prepared["commit_endpoint"], json={})
            assert response.status_code == 400
            assert "目标" in response.json()["message"]
            detail = await app.state.plugin.store.generation_detail(
                target, include_assets=False
            )
            assert len(detail["images"]) == 1

    asyncio.run(run())


def test_merge_prepare_rejects_automatic_target_and_overfull_batch(tmp_path):
    async def run():
        app = await create_app(tmp_path, seed=False)
        target = await target_record(app)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            generated = await client.post(
                PREFIX + "studio/generate",
                json={
                    "mode": "text2img",
                    "model_ref": "nai:nai-diffusion-4-5-full",
                    "prompt": "landscape",
                },
            )
            assert generated.status_code == 200
            rejected = await client.post(
                PREFIX + "imports/prepare",
                json={
                    "merge_target_id": generated.json()["generation_id"],
                    "generation_engine": "novelai",
                    "items": [{"client_id": "first", "sha256": "a" * 64}],
                },
            )
            assert rejected.status_code == 400
            assert "手动导入" in rejected.json()["message"]
            overfull = await client.post(
                PREFIX + "imports/prepare",
                json={
                    "merge_target_id": target,
                    "generation_engine": "novelai",
                    "items": [
                        {"client_id": f"item-{index}", "sha256": f"{index:064x}"}
                        for index in range(100)
                    ],
                },
            )
            assert overfull.status_code == 400
            assert "100" in overfull.json()["message"]

    asyncio.run(run())
