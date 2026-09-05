from __future__ import annotations

import asyncio
import copy
import json
import time

import httpx
import pytest

from astrbot_plugin_image_studio.tests.webui_harness import create_app, fixture_image

PREFIX = "/astrbot_plugin_image_studio/"


def group_items(models=("nai-diffusion-4-5-full", " nai-diffusion-4-5-full ")):
    return [
        {
            "client_id": f"group-item-{i}",
            "filename": f"group-{i}.png",
            "overrides": {
                "model": model,
                "prompt": f"manual group prompt {i}",
                "negative_prompt": f"negative {i}",
                "mode": "text2img",
                "parameters": {"seed": 50 + i},
            },
        }
        for i, model in enumerate(models)
    ]


@pytest.mark.parametrize("models", [("a", "b"), ("", ""), ("a", " ")])
def test_group_prepare_requires_one_nonempty_model(tmp_path, models):
    async def run():
        app = await create_app(tmp_path, seed=False)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            result = await client.post(
                PREFIX + "imports/prepare",
                json={"as_group": True, "items": group_items(models)},
            )
            assert result.status_code == 400
            assert "模型" in result.json()["message"]
            assert app.state.plugin._imports == {}
            assert app.state.plugin._import_groups == {}
            assert list(app.state.plugin.store.imports_dir.iterdir()) == []

    asyncio.run(run())


def test_group_upload_commit_retry_and_individual_parameters(tmp_path):
    async def run():
        app = await create_app(tmp_path, seed=False)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            prepared = (
                await client.post(
                    PREFIX + "imports/prepare",
                    json={"as_group": True, "items": group_items()},
                )
            ).json()
            endpoints = [PREFIX + item["upload_endpoint"] for item in prepared["items"]]
            first = await client.post(
                endpoints[0],
                files={
                    "file": ("first.png", fixture_image(0, novelai=True), "image/png")
                },
            )
            assert first.status_code == 200
            assert (await client.get(PREFIX + "gallery/list")).json()["total"] == 0
            incomplete = await client.post(
                PREFIX + prepared["commit_endpoint"], json={}
            )
            assert (
                incomplete.status_code == 400
                and "group-1.png" in incomplete.json()["message"]
            )
            malformed = await client.post(
                endpoints[1],
                files={"file": ("invalid.png", b"not an image", "image/png")},
            )
            assert malformed.status_code == 400
            assert (await client.get(PREFIX + "gallery/list")).json()["total"] == 0
            second = await client.post(
                endpoints[1],
                files={
                    "file": ("second.png", fixture_image(1, novelai=True), "image/png")
                },
            )
            assert second.status_code == 200
            group = app.state.plugin._import_groups[prepared["group_id"]]
            group["items"][1][1]["overrides"]["model"] = "different"
            assert (
                await client.post(PREFIX + prepared["commit_endpoint"], json={})
            ).status_code == 400
            assert (await client.get(PREFIX + "gallery/list")).json()["total"] == 0
            group["items"][1][1]["overrides"]["model"] = "nai-diffusion-4-5-full"
            result = await client.post(PREFIX + prepared["commit_endpoint"], json={})
            assert result.status_code == 200, result.text
            generation_id = result.json()["generation_id"]
            assert not group["directory"].exists()
            assert (
                await client.post(PREFIX + prepared["commit_endpoint"], json={})
            ).json() == result.json()
            detail = (
                await client.get(
                    PREFIX + f"gallery/detail/{generation_id}", params={"assets": 0}
                )
            ).json()
            assert len(detail["images"]) == 2
            assert (await client.get(PREFIX + "gallery/list")).json()["total"] == 1
            for index, image in enumerate(detail["images"]):
                copied = (
                    await client.get(
                        PREFIX + f"gallery/parameters/{generation_id}",
                        params={"image_id": image["id"], "format": "studio"},
                    )
                ).json()
                content = json.loads(copied["content"])
                assert content["data"]["prompt"] == f"manual group prompt {index}"
                assert content["data"]["parameters"]["seed"] == 50 + index
                assert content["data"]["negative_prompt"] == f"negative {index}"
                draft = (
                    await client.post(
                        PREFIX + f"gallery/reproduce/{generation_id}",
                        json={"image_id": image["id"]},
                    )
                ).json()
                assert draft["prompt"] == f"manual group prompt {index}"
            await client.post(PREFIX + prepared["cancel_endpoint"], json={})
            assert (await client.get(PREFIX + "gallery/list")).json()["total"] == 1

    asyncio.run(run())


def test_cancel_and_expiry_remove_only_pending_group_files(tmp_path):
    async def run():
        app = await create_app(tmp_path, seed=False)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            for expired in (False, True):
                prepared = (
                    await client.post(
                        PREFIX + "imports/prepare",
                        json={"as_group": True, "items": group_items()},
                    )
                ).json()
                upload = PREFIX + prepared["items"][0]["upload_endpoint"]
                await client.post(
                    upload, files={"file": ("first.png", fixture_image(), "image/png")}
                )
                group = app.state.plugin._import_groups[prepared["group_id"]]
                assert group["directory"].exists()
                if expired:
                    group["created_at"] -= 7200
                    await app.state.plugin._expire_import_groups()
                else:
                    assert (
                        await client.post(PREFIX + prepared["cancel_endpoint"], json={})
                    ).status_code == 200
                assert not group["directory"].exists()
                assert (
                    await client.post(PREFIX + prepared["commit_endpoint"], json={})
                ).status_code == 410
                assert (await client.get(PREFIX + "gallery/list")).json()["total"] == 0

    asyncio.run(run())


async def prepare(
    client: httpx.AsyncClient, identifier: str, overrides: dict | None = None
) -> str:
    response = await client.post(
        PREFIX + "imports/prepare",
        json={
            "items": [
                {
                    "client_id": identifier,
                    "filename": "landscape.png",
                    "overrides": overrides or {},
                }
            ]
        },
    )
    assert response.status_code == 200, response.text
    return PREFIX + response.json()["items"][0]["upload_endpoint"]


def test_import_inspect_confirm_and_retry_are_consistent(tmp_path) -> None:
    async def run() -> None:
        app = await create_app(tmp_path, seed=False)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            inspect = await client.post(
                PREFIX + "imports/inspect",
                json={
                    "metadata": {
                        "Software": "NovelAI",
                        "Comment": '{"prompt":"client claim","steps":2}',
                    },
                    "width": 1,
                    "height": 1,
                },
            )
            assert inspect.status_code == 200
            assert inspect.json()["format"] == "novelai"
            assert (await client.get(PREFIX + "gallery/list")).json()["total"] == 0
            endpoint = await prepare(
                client,
                "import-first",
                {"prompt": "人工说明", "parameters": {"seed": 78}},
            )
            upload = await client.post(
                endpoint,
                files={
                    "file": (
                        "landscape.png",
                        fixture_image(5, novelai=True),
                        "image/png",
                    )
                },
            )
            assert upload.status_code == 200, upload.text
            identifier = upload.json()["generation_id"]
            detail = (
                await client.get(
                    PREFIX + f"gallery/detail/{identifier}", params={"assets": 0}
                )
            ).json()
            assert detail["source"] == "import"
            assert detail["original_prompt"] == "人工说明"
            assert detail["images"][0]["metadata"]["normalized"]["steps"] == 26
            assert detail["supplemental"]["overrides"]["parameters"]["seed"] == 78
            assert (
                await client.post(
                    endpoint,
                    files={
                        "file": (
                            "landscape.png",
                            fixture_image(5, novelai=True),
                            "image/png",
                        )
                    },
                )
            ).status_code == 410
            retry = await prepare(client, "import-first")
            duplicate = await client.post(
                retry,
                files={
                    "file": (
                        "landscape.png",
                        fixture_image(5, novelai=True),
                        "image/png",
                    )
                },
            )
            assert duplicate.status_code == 200
            assert duplicate.json() == {"generation_id": identifier, "duplicate": True}
            assert (
                await client.get(
                    PREFIX + "gallery/list",
                    params={"source": "import", "query": "人工说明"},
                )
            ).json()["total"] == 1

    asyncio.run(run())


def test_gallery_favorite_partial_delete_and_cover_update(tmp_path) -> None:
    async def run() -> None:
        app = await create_app(tmp_path)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            gallery = (
                await client.get(PREFIX + "gallery/list", params={"limit": 25})
            ).json()
            assert len(gallery["items"]) == 25
            assert gallery["total"] == 37
            assert len(gallery["retention"]["candidate_ids"]) == 10
            identifier = app.state.seed_ids[0]
            detail = (
                await client.get(
                    PREFIX + f"gallery/detail/{identifier}", params={"assets": 0}
                )
            ).json()
            assert len(detail["images"]) == 3
            image_ids = [image["id"] for image in detail["images"]]
            favorite = await client.post(
                PREFIX + "gallery/favorite",
                json={"generation_id": identifier, "favorite": True},
            )
            assert favorite.status_code == 200
            filtered = (
                await client.get(PREFIX + "gallery/list", params={"favorite": "true"})
            ).json()
            assert [row["id"] for row in filtered["items"]] == [identifier]
            assert identifier not in filtered["retention"]["candidate_ids"]
            deleted = await client.post(
                PREFIX + "gallery/images/delete",
                json={"generation_id": identifier, "image_ids": image_ids[:1]},
            )
            assert deleted.status_code == 200
            assert deleted.json()["remaining"] == 2
            filtered = (
                await client.get(PREFIX + "gallery/list", params={"favorite": "true"})
            ).json()
            assert filtered["items"][0]["image_id"] == image_ids[1]
            updated = (
                await client.get(
                    PREFIX + f"gallery/detail/{identifier}", params={"assets": 0}
                )
            ).json()
            assert updated["parameters"]["count"] == 3
            assert len(updated["images"]) == 2
            unfavorite = await client.post(
                PREFIX + "gallery/favorite",
                json={"generation_id": identifier, "favorite": False},
            )
            assert (
                unfavorite.json()["cleanup_protected_until"] > time.time() + 23 * 3600
            )
            final = await client.post(
                PREFIX + "gallery/images/delete",
                json={"generation_id": identifier, "image_ids": image_ids[1:]},
            )
            assert final.json()["generation_deleted"]
            assert (
                await client.get(PREFIX + f"gallery/detail/{identifier}")
            ).status_code == 404

    asyncio.run(run())


def test_generate_parameter_export_resolve_and_settings_revision(tmp_path) -> None:
    async def run() -> None:
        app = await create_app(tmp_path, seed=False)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            bootstrap = (await client.get(PREFIX + "studio/bootstrap")).json()
            assert len(bootstrap["models"]) == 2
            response = await client.post(
                PREFIX + "studio/generate",
                json={
                    "mode": "text2img",
                    "model_ref": "nai:nai-diffusion-4-5-full",
                    "prompt": "mountain landscape",
                    "negative_prompt": "",
                    "parameters": {"cfg": 0, "artist": ""},
                },
            )
            assert response.status_code == 200, response.text
            result = response.json()
            assert len(result["images"]) == 2
            exported = await client.get(
                PREFIX + f"gallery/parameters/{result['generation_id']}"
            )
            assert exported.status_code == 200
            resolved = await client.post(
                PREFIX + "studio/parameters/resolve",
                json={"content": exported.json()["content"]},
            )
            assert resolved.status_code == 200, resolved.text
            assert resolved.json()["draft"]["parameters"]["cfg"] == 0
            assert resolved.json()["draft"]["negative_prompt"] == ""
            current = (await client.get(PREFIX + "settings/get")).json()
            saved = copy.deepcopy(current)
            saved["settings_revision"] = bootstrap["settings_revision"]
            saved["studio"]["history"]["max_records"] = 77
            saved["base"]["enable_llm_tool"] = False
            response = await client.post(PREFIX + "settings/save", json=saved)
            assert response.status_code == 200, response.text
            assert (
                response.json()["settings_revision"]
                == bootstrap["settings_revision"] + 1
            )
            assert (
                await client.post(PREFIX + "settings/save", json=saved)
            ).status_code == 409
            reloaded = (await client.get(PREFIX + "settings/get")).json()
            assert reloaded["studio"]["history"]["max_records"] == 77
            assert reloaded["base"]["enable_llm_tool"] is False
            assert (tmp_path / "studio_config.json").is_file()

    asyncio.run(run())


@pytest.mark.parametrize(
    "workflow",
    [
        {"nodes": [], "links": None},
        {"nodes": [{"id": 1, "type": [], "inputs": []}], "links": []},
        {"nodes": [{"id": 1, "type": "SaveImage", "inputs": None}], "links": []},
    ],
)
def test_malformed_workflow_returns_client_error_not_server_error(
    tmp_path, workflow
) -> None:
    async def run() -> None:
        app = await create_app(tmp_path, seed=False)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.post(
                PREFIX + "imports/inspect",
                json={"metadata": {"workflow": json.dumps(workflow)}},
            )
            assert response.status_code == 400
            assert response.json()["message"]

    asyncio.run(run())


def test_invalid_input_and_oversize_upload_rejected_without_persistence(
    tmp_path,
) -> None:
    async def run() -> None:
        app = await create_app(tmp_path, seed=False)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            for path, body in [
                ("imports/inspect", {"metadata": []}),
                ("imports/prepare", {"items": []}),
                (
                    "imports/prepare",
                    {"items": [{"client_id": "duplicate"}, {"client_id": "duplicate"}]},
                ),
                (
                    "imports/prepare",
                    {"items": [{"client_id": "bad", "overrides": {"parameters": []}}]},
                ),
                ("gallery/favorite", {"generation_id": "missing", "favorite": "true"}),
                (
                    "gallery/images/delete",
                    {"generation_id": "missing", "image_ids": []},
                ),
                ("studio/parameters/resolve", {"content": "ordinary prompt text"}),
            ]:
                response = await client.post(PREFIX + path, json=body)
                assert response.status_code == 400, (path, response.text)
            endpoint = await prepare(client, "oversize")
            response = await client.post(
                endpoint,
                files={
                    "file": (
                        "large.png",
                        b"x",
                        "image/png",
                        {"Content-Length": str(30 * 1024 * 1024 + 1)},
                    )
                },
            )
            assert response.status_code == 400
            assert "30 MB" in response.json()["message"]
            invalid = await client.post(
                endpoint, files={"file": ("invalid.png", b"not an image", "image/png")}
            )
            assert invalid.status_code == 400
            assert (await client.get(PREFIX + "gallery/list")).json()["total"] == 0

    asyncio.run(run())
