from __future__ import annotations

import asyncio
import json

import httpx
import pytest
from astrbot_plugin_image_studio.backend.config import HistorySettings
from astrbot_plugin_image_studio.backend.gallery.storage import (
    refresh_gallery_projection,
)
from astrbot_plugin_image_studio.backend.gallery.store import GenerationStore
from astrbot_plugin_image_studio.backend.models import (
    GeneratedImage,
    GenerationRequest,
    InvocationSource,
)
from astrbot_plugin_image_studio.tests.backend.gallery.test_external_storage import (
    add as add_external,
)
from astrbot_plugin_image_studio.tests.backend.gallery.test_external_storage import (
    setup as setup_external,
)
from astrbot_plugin_image_studio.tests.support.gallery_images import image, stage
from astrbot_plugin_image_studio.tests.backend.storage.test_storage import PNG, provider
from astrbot_plugin_image_studio.tests.support.webui_harness import create_app


async def record(store, *, created_at=100, identity=None, keep_identity=True):
    generation_id = await store.record_success(
        provider=provider(),
        request=GenerationRequest(
            mode="text2img",
            provider_id="test-provider",
            prompt="a picture",
            source="command",
            invocation_source=identity or InvocationSource(),
        ),
        images=(GeneratedImage(PNG, "image/png"),),
        elapsed_ms=1,
        history=HistorySettings(True, 0, 0, False, keep_identity),
    )
    with store._connect() as conn:
        conn.execute(
            "UPDATE generations SET created_at = ? WHERE id = ?",
            (created_at, generation_id),
        )
        refresh_gallery_projection(conn, generation_id)
    return generation_id


def test_facets_only_include_visible_records_and_keep_unknown_last(tmp_path):
    async def run():
        store, root = await setup_external(tmp_path)
        assert (await store.list_generations({}))["filters"] == {
            "providers": [],
            "modes": [],
            "sources": [],
            "generation_engines": [],
        }
        local = await record(store)
        unassigned = await store.import_image(image("blue"), "import.png", {})
        with store._connect() as conn:
            conn.execute(
                "UPDATE generations SET mode=' ',generation_engine=' ' WHERE id=?",
                (unassigned["generation_id"],),
            )
        external, _ = await add_external(store, root)
        await store.configure_external_source("nai", "NAI 插件图库", root, False)
        listing = await store.list_generations({})
        facets = listing["filters"]
        assert facets == {
            "providers": [
                {"id": "test-provider", "name": "Test Provider"},
                {"id": "", "name": "未指定"},
            ],
            "modes": ["text2img", "unknown"],
            "sources": ["command", "import"],
            "generation_engines": ["openai_images", "unknown"],
        }
        assert (await store.list_generations({"sources": []}))["filters"] == facets
        unknown = await store.list_generations(
            {
                "modes": ["unknown"],
                "generation_engines": ["unknown"],
                "provider_ids": [""],
            }
        )
        assert [item["id"] for item in unknown["items"]] == [
            unassigned["generation_id"]
        ]
        assert (await store.list_generations({"query": "no matches"}))[
            "filters"
        ] == facets
        await store.configure_external_source("nai", "NAI 插件图库", root, True)
        assert "external" in (await store.list_generations({}))["filters"]["sources"]
        # Local duplicates hide external cards and their otherwise unique facets.
        await store.import_image(image("red"), "different.png", {})
        external_data = (root / "nai_1.png").read_bytes()
        await store.configure_external_source("nai", "NAI 插件图库", root, False)
        await store.import_image(external_data, "same-external.png", {})
        await store.configure_external_source("nai", "NAI 插件图库", root, True)
        assert (
            "external" not in (await store.list_generations({}))["filters"]["sources"]
        )
        # A historical row without output images cannot contribute phantom options
        # or inflate pagination totals.
        with store._connect() as conn:
            conn.execute(
                "DELETE FROM generation_images WHERE generation_id=?", (local,)
            )
            conn.execute(
                "UPDATE generations SET provider_id='empty-only', mode='img2img', generation_engine='empty-only' WHERE id=?",
                (local,),
            )
        listing = await store.list_generations({})
        assert listing["total"] == len(listing["items"])
        assert "empty-only" not in json.dumps(listing["filters"])
        assert "img2img" not in listing["filters"]["modes"]
        assert external["generation_id"] not in [
            item["id"] for item in listing["items"]
        ]

    asyncio.run(run())


def test_identity_search_matches_saved_context_and_adapter_labels_only(tmp_path):
    async def run():
        store = GenerationStore(tmp_path)
        await store.initialize()
        group = await record(
            store,
            identity=InvocationSource(
                context_type="group",
                platform_name="aiocqhttp",
                platform_id="bot-instance-42",
                group_id="group-52",
                group_name="绘画小组",
                user_id="user-62",
                user_name="小绘师",
            ),
        )
        private = await record(
            store,
            identity=InvocationSource(
                context_type="private",
                platform_name="qq_official",
                platform_id="official-bot",
                user_id="private-user",
                user_name="小蓝",
            ),
        )
        await record(
            store,
            identity=InvocationSource(
                context_type="private",
                platform_name="qq_official",
                user_id="secret-user",
            ),
            keep_identity=False,
        )
        with store._connect() as conn:
            conn.execute(
                "UPDATE generations SET parameters_json=?",
                ('{"api_key":"credential-should-never-match"}',),
            )
        for query in (
            "群聊",
            "group",
            "aiocqhttp",
            "OneBot v11",
            "bot-instance-42",
            "group-52",
            "绘画小组",
            "user-62",
            "小绘师",
        ):
            listing = await store.list_generations({"query": query})
            assert [item["id"] for item in listing["items"]] == [group], query
            assert [
                item["generation_id"]
                for item in await store.gallery_image_sequence({"query": query})
            ] == [group]
        for query in ("私聊", "private", "qq_official", "QQ官方", "小蓝"):
            assert [
                item["id"]
                for item in (await store.list_generations({"query": query}))["items"]
            ] == [private], query
        for query in ("secret-user", "credential-should-never-match"):
            assert (await store.list_generations({"query": query}))["total"] == 0

    asyncio.run(run())


def test_latest_content_sort_tracks_image_dates_after_append_edit_and_delete(tmp_path):
    async def run():
        store = GenerationStore(tmp_path)
        await store.initialize()
        group = await store.import_group(
            [
                stage(store, image("red"), "first.png", generated_at=100),
                stage(store, image("blue"), "second.png", generated_at=300),
            ],
            import_key="group",
        )
        group_id = group["generation_id"]
        later = await record(store, created_at=200)
        with store._connect() as conn:
            conn.execute("UPDATE generations SET created_at=50 WHERE id=?", (group_id,))
            refresh_gallery_projection(conn, group_id)

        async def check(expected, expected_time):
            listing = await store.list_generations({"sort": "latest_content"})
            assert [item["id"] for item in listing["items"]] == expected
            assert (
                next(
                    item["sort_time"]
                    for item in listing["items"]
                    if item["id"] == group_id
                )
                == expected_time
            )
            sequence = await store.gallery_image_sequence({"sort": "latest_content"})
            assert (
                list(dict.fromkeys(item["generation_id"] for item in sequence))
                == expected
            )
            assert {
                item["sort_time"]
                for item in sequence
                if item["generation_id"] == group_id
            } == {expected_time}
            page = await store.list_generations(
                {"sort": "latest_content", "limit": 1, "offset": 1}
            )
            assert page["items"][0]["id"] == expected[1]
            assert [
                item["id"] for item in (await store.list_generations({}))["items"]
            ] == [later, group_id]
            assert (await store.generation_detail(group_id, light=True))[
                "created_at"
            ] == 50

        await check([group_id, later], 300)
        snapshot = await store.import_edit_snapshot(group_id)
        await store.edit_import(
            group_id,
            snapshot["revision"],
            [
                {
                    "image_id": item["image_id"],
                    "overrides": {"generated_at": 100 + index * 50},
                }
                for index, item in enumerate(reversed(snapshot["items"]))
            ],
        )
        await check([later, group_id], 150)
        await store.append_import_group(
            [
                stage(store, image("green"), "third.png", generated_at=500),
            ],
            group_id,
            import_key="append",
            expected_engine="novelai",
        )
        await check([group_id, later], 500)
        detail = await store.generation_detail(group_id, light=True)
        await store.delete_images(group_id, [detail["images"][-1]["id"]])
        await check([later, group_id], 150)

    asyncio.run(run())


def test_latest_content_uses_per_record_fallback_and_external_chosen_time(tmp_path):
    async def run():
        store, root = await setup_external(tmp_path)
        old = await record(store, created_at=100)
        new = await record(store, created_at=300)
        external, _ = await add_external(store, root)
        with store._connect() as conn:
            conn.execute("UPDATE image_assets SET created_at=900")
            conn.execute(
                "UPDATE generations SET created_at=200 WHERE id=?",
                (external["generation_id"],),
            )
            conn.execute(
                "UPDATE generation_images SET supplemental_json=? WHERE generation_id=?",
                ('{"generated_at":800}', external["generation_id"]),
            )
            refresh_gallery_projection(conn, external["generation_id"])
        for invalid in (None, "700", True, -1, 253402300800, float("inf")):
            with store._connect() as conn:
                # SQLite JSON accepts numeric overflow as infinity, but it must
                # never be accepted as a sorting timestamp.
                payload = (
                    '{"generated_at":1e999}'
                    if invalid == float("inf")
                    else json.dumps({"generated_at": invalid})
                )
                conn.execute(
                    "UPDATE generation_images SET supplemental_json=? WHERE generation_id=?",
                    (payload, new),
                )
                refresh_gallery_projection(conn, new)
            listing = await store.list_generations({"sort": "latest_content"})
            assert [(item["id"], item["sort_time"]) for item in listing["items"]] == [
                (new, 300),
                (external["generation_id"], 200),
                (old, 100),
            ]

    asyncio.run(run())


def test_gallery_sort_api_validates_and_keeps_list_and_sequence_consistent(tmp_path):
    async def run():
        app = await create_app(tmp_path, seed=False)
        store = app.state.plugin.store
        first, second = (
            await record(store, created_at=100),
            await record(store, created_at=200),
        )
        with store._connect() as conn:
            conn.execute(
                "UPDATE generation_images SET supplemental_json=? WHERE generation_id=?",
                ('{"generated_at":300}', first),
            )
            refresh_gallery_projection(conn, first)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            prefix = "/astrbot_plugin_image_studio/gallery/"
            for sort, expected in (
                ("created", [second, first]),
                ("latest_content", [first, second]),
            ):
                listing = await client.get(prefix + "list", params={"sort": sort})
                sequence = await client.get(
                    prefix + "image-sequence", params={"sort": sort}
                )
                assert listing.status_code == sequence.status_code == 200
                assert [item["id"] for item in listing.json()["items"]] == expected
                assert [
                    item["generation_id"] for item in sequence.json()["items"]
                ] == expected
            for endpoint in ("list", "image-sequence"):
                for value in ("", "recent", "created DESC; --"):
                    response = await client.get(
                        prefix + endpoint, params={"sort": value}
                    )
                    assert response.status_code == 400
            for value in (None, [], {}, 1):
                with pytest.raises(ValueError, match="排序"):
                    await store.list_generations({"sort": value})

    asyncio.run(run())
