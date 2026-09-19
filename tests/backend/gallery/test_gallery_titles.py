"""Group titles persist separately from generated/imported metadata and search."""

from __future__ import annotations

import asyncio
import hashlib
import sqlite3
from contextlib import closing

import httpx
import pytest

from astrbot_plugin_image_studio.backend.database import schema
from astrbot_plugin_image_studio.backend.database.migrations import v4_storage
from astrbot_plugin_image_studio.tests.support.webui_harness import create_app

PREFIX = "/astrbot_plugin_image_studio/"


def test_titles_default_empty_save_search_clear_and_preserve_generation_data(tmp_path):
    async def run():
        app = await create_app(tmp_path)
        store = app.state.plugin.store
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            cards = (
                await client.get(PREFIX + "gallery/list", params={"light": 1})
            ).json()["items"]
            assert all(card["title"] == "" for card in cards)
            identity = cards[0]["id"]
            with store._connect() as conn:
                before = dict(
                    conn.execute(
                        "SELECT * FROM generations WHERE id=?", (identity,)
                    ).fetchone()
                )
                immutable = {
                    table: list(map(tuple, conn.execute("SELECT * FROM " + table)))
                    for table in (
                        "generation_images",
                        "image_assets",
                        "image_metadata",
                        "storage_payloads",
                    )
                }
            original_files = {
                p: hashlib.sha256(p.read_bytes()).digest()
                for p in store.images_dir.rglob("*")
                if p.is_file()
            }
            saved = await client.post(
                PREFIX + "gallery/title",
                json={"generation_id": identity, "title": "  夏日标题 <b>猫</b> 😀  "},
            )
            assert saved.status_code == 200
            assert saved.json() == {"id": identity, "title": "夏日标题 <b>猫</b> 😀"}
            found = (
                await client.get(
                    PREFIX + "gallery/list", params={"query": "夏日标题", "light": 1}
                )
            ).json()
            assert [card["id"] for card in found["items"]] == [identity]
            assert found["items"][0]["title"] == saved.json()["title"]
            for light in (0, 1):
                detail = (
                    await client.get(
                        PREFIX + f"gallery/detail/{identity}", params={"light": light}
                    )
                ).json()
                assert detail["title"] == saved.json()["title"]
            image_id = detail["images"][0]["id"]
            metadata = (
                await client.get(PREFIX + f"gallery/image-info/{image_id}")
            ).json()
            assert metadata["detail_fields"]["title"] == saved.json()["title"]
            with store._connect() as conn:
                after = dict(
                    conn.execute(
                        "SELECT * FROM generations WHERE id=?", (identity,)
                    ).fetchone()
                )
                assert {**after, "title": ""} == before
                for table, values in immutable.items():
                    assert (
                        list(map(tuple, conn.execute("SELECT * FROM " + table)))
                        == values
                    )
            assert all(
                hashlib.sha256(path.read_bytes()).digest() == digest
                for path, digest in original_files.items()
            )
            cleared = await client.post(
                PREFIX + "gallery/title",
                json={"generation_id": identity, "title": "   "},
            )
            assert cleared.json()["title"] == ""
            assert (
                await client.get(PREFIX + "gallery/list", params={"query": "夏日标题"})
            ).json()["total"] == 0
            assert (await store.generation_detail(identity, include_assets=False))[
                "model"
            ] == before["model"]

    asyncio.run(run())


@pytest.mark.parametrize(
    "title", [None, False, 1, [], {}, "a" * 201, "first\nsecond", "first\0second"]
)
def test_invalid_titles_do_not_change_history(tmp_path, title):
    async def run():
        app = await create_app(tmp_path)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            identity = (
                await client.get(PREFIX + "gallery/list", params={"light": 1})
            ).json()["items"][0]["id"]
            response = await client.post(
                PREFIX + "gallery/title",
                json={"generation_id": identity, "title": title},
            )
            assert response.status_code == 400
            assert (
                await app.state.plugin.store.generation_detail(
                    identity, include_assets=False
                )
            )["title"] == ""
            assert (
                await client.post(
                    PREFIX + "gallery/title",
                    json={"generation_id": "missing", "title": "x"},
                )
            ).status_code == 400

    asyncio.run(run())


def create_dev1(conn):
    for statement in schema.V4_DEV1_SCHEMA_STATEMENTS:
        conn.execute(statement)
    conn.execute(schema._DEVELOPMENT_META_STATEMENT)
    conn.execute("INSERT INTO schema_meta VALUES(1,4,1)")
    conn.execute("PRAGMA user_version=3")
    conn.execute(
        "INSERT INTO generations(id,created_at,source,status,mode,provider_id,provider_name,provider_kind,model,original_prompt,final_prompt,parameters_json,elapsed_ms) VALUES('old',1,'webui','succeeded','text2img','p','provider','comfyui','workflow','prompt','prompt','{}',0)"
    )
    conn.commit()


def test_dev1_title_migration_is_backed_up_and_does_not_reconvert_payloads(
    tmp_path, monkeypatch
):
    with closing(sqlite3.connect(tmp_path / "history.sqlite3")) as conn:
        create_dev1(conn)
        before = tuple(conn.iterdump())

        def unexpected(*_args):
            pytest.fail("adding a title must not repeat the frozen payload conversion")

        monkeypatch.setattr(v4_storage, "migrate_comfy_storage", unexpected)
        monkeypatch.setattr(v4_storage, "migrate_gallery_storage", unexpected)
        backup = schema.ensure_release_schema(conn, backup_dir=tmp_path / "backups")
        assert backup is not None
        assert (
            conn.execute("SELECT title FROM generations WHERE id='old'").fetchone()[0]
            == ""
        )
        assert conn.execute(
            "SELECT target_version,dev_revision FROM schema_meta"
        ).fetchone() == (4, 2)
        with closing(sqlite3.connect(backup)) as original:
            assert tuple(original.iterdump()) == before
        state = tuple(conn.iterdump())
        assert (
            schema.ensure_release_schema(conn, backup_dir=tmp_path / "backups") is None
        )
        assert tuple(conn.iterdump()) == state


def test_title_schema_upgrade_rolls_back_on_failure(tmp_path, monkeypatch):
    with closing(sqlite3.connect(tmp_path / "history.sqlite3")) as conn:
        create_dev1(conn)
        before = tuple(conn.iterdump())
        monkeypatch.setattr(
            schema,
            "V4_DEV2_MIGRATION_STATEMENTS",
            (*schema.V4_DEV2_MIGRATION_STATEMENTS, "invalid migration SQL"),
        )
        with pytest.raises(sqlite3.Error):
            schema.ensure_release_schema(conn, backup_dir=tmp_path / "backups")
        assert tuple(conn.iterdump()) == before
        assert not conn.in_transaction
