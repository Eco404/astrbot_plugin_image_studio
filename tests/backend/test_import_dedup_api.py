from __future__ import annotations

import asyncio
import hashlib

import httpx
import pytest

from astrbot_plugin_image_studio.tests.backend.test_gallery_api import PREFIX
from astrbot_plugin_image_studio.tests.backend.test_import_groups import image
from astrbot_plugin_image_studio.tests.support.webui_harness import create_app


def items_for(images, overrides=None):
    return [
        {
            "client_id": f"item-{index}",
            "filename": f"file-{index}.png",
            "sha256": hashlib.sha256(data).hexdigest(),
            "overrides": (overrides or [{} for _ in images])[index],
        }
        for index, data in enumerate(images)
    ]


async def prepared(client, images, *, overrides=None, **options):
    response = await client.post(
        PREFIX + "imports/prepare",
        json={"items": items_for(images, overrides), **options},
    )
    assert response.status_code == 200 and response.json()["allowed"], response.text
    return response.json()


async def upload_all(client, batch, images):
    for item, data in zip(batch["items"], images):
        response = await client.post(
            PREFIX + item["upload_endpoint"],
            files={"file": ("image.png", data, "image/png")},
        )
        assert response.status_code == 200 and response.json().get("uploaded"), (
            response.text
        )


def test_check_and_prepare_reject_gallery_duplicate_whole_batch_without_tickets(
    tmp_path,
):
    async def run():
        app = await create_app(tmp_path, seed=False)
        images = [image("red"), image("blue")]
        await app.state.plugin.store.import_image(images[0], "existing.png", {})
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            for endpoint in ("imports/check", "imports/prepare"):
                response = await client.post(
                    PREFIX + endpoint, json={"items": items_for(images)}
                )
                assert response.status_code == 200
                payload = response.json()
                assert (
                    payload["allowed"] is False
                    and payload["code"] == "gallery_duplicates"
                )
                assert payload["duplicate_hashes"] == [
                    hashlib.sha256(images[0]).hexdigest()
                ]
                assert "status" not in payload
                assert "group_id" not in payload and "items" not in payload
            assert (
                not app.state.plugin._import_api().uploads
                and not app.state.plugin._import_api().groups
            )
            assert not list(app.state.plugin.store.imports_dir.iterdir())
            assert (await client.get(PREFIX + "gallery/list")).json()["total"] == 1

    asyncio.run(run())


def test_check_rejects_intra_batch_duplicates_and_accepts_uppercase_hash(tmp_path):
    async def run():
        app = await create_app(tmp_path, seed=False)
        data = image("red")
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            duplicates = items_for([data, data])
            duplicates[0]["sha256"] = duplicates[0]["sha256"].upper()
            response = await client.post(
                PREFIX + "imports/check", json={"items": duplicates}
            )
            assert response.status_code == 200
            assert response.json()["code"] == "batch_duplicates"
            assert response.json()["duplicate_hashes"] == [
                hashlib.sha256(data).hexdigest()
            ]
            response = await client.post(
                PREFIX + "imports/prepare", json={"items": duplicates}
            )
            assert response.json()["allowed"] is False
            valid = await client.post(
                PREFIX + "imports/check", json={"items": duplicates[:1]}
            )
            assert valid.json()["allowed"] is True

    asyncio.run(run())


@pytest.mark.parametrize(
    "digest", [None, "", "a" * 63, "z" * 64, 1, [], " a" + "b" * 62]
)
def test_hash_validation_rejects_malformed_declarations(tmp_path, digest):
    async def run():
        app = await create_app(tmp_path, seed=False)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            for endpoint in ("imports/check", "imports/prepare"):
                response = await client.post(
                    PREFIX + endpoint,
                    json={"items": [{"client_id": "image", "sha256": digest}]},
                )
                assert response.status_code == 400
            assert (
                not app.state.plugin._import_api().uploads
                and not app.state.plugin._import_api().groups
            )

    asyncio.run(run())


def test_separate_imports_share_staging_and_commit_all_records_in_order(tmp_path):
    async def run():
        app = await create_app(tmp_path, seed=False)
        images = [image("red", seed=1), image("blue", seed=2)]
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            batch = await prepared(client, images)
            assert "commit_endpoint" in batch and "cancel_endpoint" in batch
            await upload_all(client, batch, images)
            assert (await client.get(PREFIX + "gallery/list")).json()["total"] == 0
            response = await client.post(PREFIX + batch["commit_endpoint"], json={})
            assert response.status_code == 200, response.text
            payload = response.json()
            assert payload["allowed"] and payload["mode"] == "separate"
            assert len(payload["generation_ids"]) == 2
            assert payload["added_count"] == 2
            seeds = []
            for generation_id in payload["generation_ids"]:
                detail = (
                    await client.get(
                        PREFIX + f"gallery/detail/{generation_id}", params={"assets": 0}
                    )
                ).json()
                seeds.append(detail["images"][0]["metadata"]["normalized"]["seed"])
            assert seeds == [1, 2]
            assert (
                await client.post(PREFIX + batch["commit_endpoint"], json={})
            ).json() == payload
            assert not list(app.state.plugin.store.imports_dir.iterdir())

    asyncio.run(run())


def test_upload_must_match_declared_hash_and_can_retry_original_bytes(tmp_path):
    async def run():
        app = await create_app(tmp_path, seed=False)
        images = [image("red")]
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            batch = await prepared(client, images)
            mismatch = await client.post(
                PREFIX + batch["items"][0]["upload_endpoint"],
                files={"file": ("changed.png", image("blue"), "image/png")},
            )
            assert mismatch.status_code == 200
            assert mismatch.json()["allowed"] is False
            assert mismatch.json()["code"] == "hash_mismatch"
            assert "status" not in mismatch.json()
            assert (
                app.state.plugin._import_api().groups[batch["group_id"]]["items"][0][1][
                    "uploaded"
                ]
                is False
            )
            incomplete = await client.post(PREFIX + batch["commit_endpoint"], json={})
            assert incomplete.status_code == 400
            await upload_all(client, batch, images)
            assert (
                await client.post(PREFIX + batch["commit_endpoint"], json={})
            ).json()["allowed"]

    asyncio.run(run())


@pytest.mark.parametrize("mode", ["separate", "group", "merge"])
def test_duplicate_arriving_after_prepare_blocks_entire_commit(tmp_path, mode):
    async def run():
        app = await create_app(tmp_path, seed=False)
        images = [image("red"), image("blue")]
        options = {}
        overrides = None
        if mode == "group":
            options["as_group"] = True
            overrides = [{"model": "same-model"}, {"model": "same-model"}]
        if mode == "merge":
            target = await app.state.plugin.store.import_image(
                image("green"), "target.png", {}
            )
            options.update(
                merge_target_id=target["generation_id"], generation_engine="novelai"
            )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            batch = await prepared(client, images, overrides=overrides, **options)
            await upload_all(client, batch, images)
            await app.state.plugin.store.import_image(
                images[0], "arrived-elsewhere.png", {}
            )
            response = await client.post(PREFIX + batch["commit_endpoint"], json={})
            assert response.status_code == 200
            payload = response.json()
            assert (
                payload["allowed"] is False and payload["code"] == "gallery_duplicates"
            )
            assert payload["duplicate_hashes"] == [
                hashlib.sha256(images[0]).hexdigest()
            ]
            assert batch["group_id"] not in app.state.plugin._import_api().groups
            assert not list(app.state.plugin.store.imports_dir.iterdir())
            assert (await client.get(PREFIX + "gallery/list")).json()["total"] == (
                2 if mode == "merge" else 1
            )

    asyncio.run(run())


def test_prepare_rechecks_after_successful_preflight(tmp_path):
    async def run():
        app = await create_app(tmp_path, seed=False)
        images = [image("red")]
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            assert (
                await client.post(
                    PREFIX + "imports/check", json={"items": items_for(images)}
                )
            ).json()["allowed"]
            await app.state.plugin.store.import_image(images[0], "arrived.png", {})
            response = await client.post(
                PREFIX + "imports/prepare", json={"items": items_for(images)}
            )
            assert response.json()["code"] == "gallery_duplicates"
            assert not app.state.plugin._import_api().uploads

    asyncio.run(run())


def test_disk_error_keeps_batch_for_retry_but_invalid_metadata_leaves_no_partial_records(
    tmp_path, monkeypatch
):
    async def run():
        app = await create_app(tmp_path, seed=False)
        images = [image("red"), image("blue")]
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            batch = await prepared(client, images)
            await upload_all(client, batch, images)
            original = app.state.plugin.store.commit_import_batch

            async def failed(*args, **kwargs):
                raise OSError("synthetic disk failure")

            monkeypatch.setattr(app.state.plugin.store, "commit_import_batch", failed)
            response = await client.post(PREFIX + batch["commit_endpoint"], json={})
            assert response.status_code == 500
            assert batch["group_id"] in app.state.plugin._import_api().groups
            assert (await client.get(PREFIX + "gallery/list")).json()["total"] == 0
            monkeypatch.setattr(app.state.plugin.store, "commit_import_batch", original)
            assert (
                await client.post(PREFIX + batch["commit_endpoint"], json={})
            ).json()["allowed"]
            invalid_images = [image("green"), image("yellow")]
            batch = await prepared(
                client, invalid_images, overrides=[{}, {"mode": "invalid-mode"}]
            )
            await upload_all(client, batch, invalid_images)
            response = await client.post(PREFIX + batch["commit_endpoint"], json={})
            assert response.status_code == 400
            assert batch["group_id"] not in app.state.plugin._import_api().groups
            assert (await client.get(PREFIX + "gallery/list")).json()["total"] == 2

    asyncio.run(run())


def test_successful_commit_survives_restart_and_never_recreates_deleted_records(
    tmp_path,
):
    async def run():
        app = await create_app(tmp_path, seed=False)
        images = [image("red"), image("blue")]
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            batch = await prepared(client, images)
            await upload_all(client, batch, images)
            committed = await client.post(PREFIX + batch["commit_endpoint"], json={})
            assert committed.status_code == 200
            saved = committed.json()
        restarted = await create_app(tmp_path, seed=False)
        assert restarted.state.plugin._import_api().groups == {}
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=restarted), base_url="http://test"
        ) as client:
            replayed = await client.post(PREFIX + batch["commit_endpoint"], json={})
            assert replayed.status_code == 200
            assert replayed.json() == saved
            assert (await client.get(PREFIX + "gallery/list")).json()["total"] == 2
            for identifier in saved["generation_ids"]:
                await restarted.state.plugin.store.delete_generation(identifier)
            assert (
                await client.post(PREFIX + batch["commit_endpoint"], json={})
            ).json() == saved
            assert (await client.get(PREFIX + "gallery/list")).json()["total"] == 0
            assert not list(restarted.state.plugin.store.imports_dir.iterdir())
            invalid = await client.post(
                PREFIX + "imports/group/not-a-batch/commit", json={}
            )
            assert invalid.status_code == 400

    asyncio.run(run())
