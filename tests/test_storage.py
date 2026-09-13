from __future__ import annotations

import asyncio
import base64
import io
import json
import re
import sqlite3
import time
import zipfile
from pathlib import Path

import pytest
from astrbot_plugin_image_studio.config import HistorySettings
from astrbot_plugin_image_studio.models import (
    GeneratedImage,
    GenerationRequest,
    ImageProvider,
    InvocationSource,
    ReferenceImage,
)
from astrbot_plugin_image_studio.storage import GenerationStore

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


def test_workflow_lease_reuses_canonical_asset_and_shared_preview(tmp_path) -> None:
    async def run() -> None:
        from PIL import Image

        source = Image.effect_noise((1024, 768), 96).convert("RGB")
        output = io.BytesIO()
        source.save(output, "PNG")
        original_data = output.getvalue()

        store = GenerationStore(tmp_path)
        await store.initialize()
        await store.record_success(
            provider=provider(),
            request=GenerationRequest(
                mode="text2img", provider_id="test-provider", prompt="noise"
            ),
            images=(GeneratedImage(original_data, "image/png"),),
            elapsed_ms=10,
            history=HistorySettings(True, 10, 50, False),
        )
        history_original = next(store.assets_dir.rglob("*.png"))
        assets = await store.lease_agent_images(
            (GeneratedImage(original_data, "image/png"),),
            scope_id="test:private:user-1",
            create_preview=True,
            preview_max_edge=320,
            preview_quality=75,
        )

        assert len(assets) == 1
        asset = assets[0]
        assert re.fullmatch(r"[a-f0-9]{64}", asset.asset_id)
        with store._connect() as conn:
            access = conn.execute("SELECT * FROM agent_asset_leases").fetchone()
        assert (
            access["expires_at"]
            == access["hard_expires_at"]
            == access["last_accessed_at"] + 3600
        )
        assert history_original.read_bytes() == original_data
        assert asset.preview is not None
        assert len(asset.preview.data) < len(original_data)
        with Image.open(io.BytesIO(asset.preview.data)) as preview:
            assert max(preview.size) <= 320

        loaded_preview = await store.load_workflow_image(
            asset.asset_id,
            scope_id="test:private:user-1",
            detail="preview",
            preview_max_edge=320,
            preview_quality=75,
        )
        loaded_original = await store.load_workflow_image(
            asset.asset_id,
            scope_id="test:private:user-1",
            detail="original",
            preview_max_edge=320,
            preview_quality=75,
        )
        assert loaded_preview is not None and loaded_original is not None
        assert loaded_preview[0].data == asset.preview.data
        assert loaded_original[0].data == original_data
        assert Path(loaded_original[1]) == history_original
        assert len(list(store.assets_dir.rglob("*.png"))) == 1
        assert len(list(store.thumbnails_dir.rglob("*.webp"))) == 1

        denied = await store.load_workflow_image(
            asset.asset_id,
            scope_id="test:private:user-2",
            detail="preview",
            preview_max_edge=320,
            preview_quality=75,
        )
        assert denied is None

    asyncio.run(run())


def test_workflow_asset_lookup_preserves_specific_failure_reasons(tmp_path) -> None:
    async def run() -> None:
        store = GenerationStore(tmp_path)
        await store.initialize()
        assets = await store.lease_agent_images(
            (GeneratedImage(PNG, "image/png"),),
            scope_id="test:private:user-1",
            create_preview=False,
            preview_max_edge=320,
            preview_quality=75,
        )
        asset_id = assets[0].asset_id
        lookup = {
            "scope_id": "test:private:user-1",
            "detail": "original",
            "preview_max_edge": 320,
            "preview_quality": 75,
        }

        invalid = await store.load_workflow_image_detailed("bad-id", **lookup)
        missing = await store.load_workflow_image_detailed(f"{0:064x}", **lookup)
        denied = await store.load_workflow_image_detailed(
            asset_id, **{**lookup, "scope_id": "test:private:user-2"}
        )
        assert invalid.status == "invalid_asset_id"
        assert missing.status == "not_found"
        assert denied.status == "access_denied"

        with store._connect() as conn:
            conn.execute(
                "UPDATE agent_asset_leases SET expires_at = 0, hard_expires_at = 0 WHERE asset_id = ?",
                (asset_id,),
            )
        retained = await store.load_workflow_image_detailed(asset_id, **lookup)
        assert retained.status == "ok"
        asset_path = next(store.assets_dir.rglob("*.png"))
        asset_path.unlink()
        file_missing = await store.load_workflow_image_detailed(asset_id, **lookup)
        assert file_missing.status == "file_missing"

        asset_path.write_bytes(b"not-an-image")
        decode_failed = await store.load_workflow_image_detailed(asset_id, **lookup)
        assert decode_failed.status == "decode_failed"

    asyncio.run(run())


def test_maintenance_expires_lease_only_assets_and_ignores_legacy_directory(
    tmp_path,
) -> None:
    async def run() -> None:
        store = GenerationStore(tmp_path)
        await store.initialize()
        legacy = tmp_path / "agent_assets" / "originals" / "legacy.png"
        legacy.parent.mkdir(parents=True)
        legacy.write_bytes(PNG)
        assets = await store.lease_agent_images(
            (GeneratedImage(PNG, "image/png"),),
            scope_id="test:private:user-1",
            create_preview=True,
            preview_max_edge=320,
            preview_quality=75,
        )
        with store._connect() as conn:
            conn.execute(
                "UPDATE agent_asset_leases SET expires_at = ?, hard_expires_at = ?",
                (time.time() - 1, time.time() - 1),
            )

        report = await store.run_maintenance(
            HistorySettings(False, 0, 0, False),
            preview_max_edge=320,
            preview_quality=75,
        )

        assert report["status"] == "healthy"
        assert report["repaired"]["expired_leases"] == 1
        assert not list(store.assets_dir.rglob("*.png"))
        assert not list(store.thumbnails_dir.rglob("*.webp"))
        assert legacy.exists()
        with store._connect() as conn:
            assert (
                conn.execute("SELECT COUNT(*) FROM agent_asset_leases").fetchone()[0]
                == 0
            )

        # Re-creating the same content must not resurrect access for the old scope.
        recreated = await store.lease_agent_images(
            (GeneratedImage(PNG, "image/png"),),
            scope_id="test:private:user-2",
            create_preview=False,
            preview_max_edge=320,
            preview_quality=75,
        )
        assert recreated[0].asset_id == assets[0].asset_id
        denied = await store.load_workflow_image_detailed(
            recreated[0].asset_id,
            scope_id="test:private:user-1",
            detail="original",
            preview_max_edge=320,
            preview_quality=75,
        )
        assert denied.status == "access_denied"

    asyncio.run(run())


def test_active_lease_preserves_asset_after_gallery_record_is_deleted(tmp_path) -> None:
    async def run() -> None:
        store = GenerationStore(tmp_path)
        await store.initialize()
        generation_id = await store.record_success(
            provider=provider(),
            request=GenerationRequest(
                mode="text2img", provider_id="test-provider", prompt="leased"
            ),
            images=(GeneratedImage(PNG, "image/png"),),
            elapsed_ms=10,
            history=HistorySettings(True, 10, 50, False),
            preview_max_edge=320,
            preview_quality=75,
        )
        assets = await store.lease_agent_images(
            (GeneratedImage(PNG, "image/png"),),
            scope_id="test:private:user-1",
            create_preview=True,
            preview_max_edge=320,
            preview_quality=75,
        )

        assert await store.delete_generation(generation_id) is True
        asset_path = next(store.assets_dir.rglob("*.png"))
        assert asset_path.exists()
        assert (
            await store.load_workflow_image(
                assets[0].asset_id,
                scope_id="test:private:user-1",
                detail="original",
                preview_max_edge=320,
                preview_quality=75,
            )
            is not None
        )

    asyncio.run(run())


def test_deep_maintenance_detects_same_size_asset_corruption(tmp_path) -> None:
    async def run() -> None:
        store = GenerationStore(tmp_path)
        await store.initialize()
        await store.lease_agent_images(
            (GeneratedImage(PNG, "image/png"),),
            scope_id="test:private:user-1",
            create_preview=True,
            preview_max_edge=320,
            preview_quality=75,
        )
        path = next(store.assets_dir.rglob("*.png"))
        path.write_bytes(b"x" * len(PNG))

        shallow = await store.run_maintenance(
            HistorySettings(False, 0, 0, False),
            preview_max_edge=320,
            preview_quality=75,
        )
        assert shallow["repaired"]["broken_assets"] == 0
        assert path.exists()

        deep = await store.run_maintenance(
            HistorySettings(False, 0, 0, False),
            preview_max_edge=320,
            preview_quality=75,
            deep=True,
        )
        assert deep["repaired"]["broken_assets"] == 1
        assert not path.exists()

    asyncio.run(run())


def test_asset_access_renews_one_hour_retention_even_after_old_hard_expiry(
    tmp_path,
) -> None:
    async def run() -> None:
        store = GenerationStore(tmp_path)
        await store.initialize()
        assets = await store.lease_agent_images(
            (GeneratedImage(PNG, "image/png"),),
            scope_id="test:private:user-1",
            create_preview=False,
            preview_max_edge=320,
            preview_quality=75,
        )
        old_access = time.time() - 8 * 24 * 3600
        with store._connect() as conn:
            conn.execute(
                "UPDATE agent_asset_leases SET last_accessed_at = ?, expires_at = ?, hard_expires_at = ?",
                (old_access, old_access + 3600, old_access + 7 * 24 * 3600),
            )

        before_read = time.time()
        loaded = await store.load_workflow_image(
            assets[0].asset_id,
            scope_id="test:private:user-1",
            detail="original",
            preview_max_edge=320,
            preview_quality=75,
        )
        with store._connect() as conn:
            lease = conn.execute(
                "SELECT last_accessed_at, expires_at, hard_expires_at FROM agent_asset_leases"
            ).fetchone()

        assert loaded is not None
        assert before_read <= lease["last_accessed_at"] <= time.time()
        assert lease["expires_at"] == lease["last_accessed_at"] + 3600
        assert lease["hard_expires_at"] == lease["expires_at"]

    asyncio.run(run())


@pytest.mark.parametrize("owner", ["gallery", "reference"])
def test_archived_workflow_asset_remains_accessible_after_retention_and_restart(
    tmp_path, owner
) -> None:
    async def run() -> None:
        from PIL import Image

        store = GenerationStore(tmp_path)
        await store.initialize()
        output = io.BytesIO()
        Image.new("RGB", (2, 2), "blue").save(output, "PNG")
        generation_id = await store.record_success(
            provider=provider(),
            request=GenerationRequest(
                mode="img2img" if owner == "reference" else "text2img",
                provider_id="test-provider",
                prompt="retained by archive",
                references=(ReferenceImage("reference", "ref.png", PNG, "image/png"),)
                if owner == "reference"
                else (),
            ),
            images=(
                GeneratedImage(
                    output.getvalue() if owner == "reference" else PNG, "image/png"
                ),
            ),
            elapsed_ms=10,
            history=HistorySettings(True, 10, 50, True),
        )
        assets = await store.lease_agent_images(
            (GeneratedImage(PNG, "image/png"),),
            scope_id="test:private:user-1",
            create_preview=True,
            preview_max_edge=320,
            preview_quality=75,
        )
        asset_id = assets[0].asset_id
        with store._connect() as conn:
            conn.execute(
                "UPDATE agent_asset_leases SET last_accessed_at = ?, expires_at = ?, hard_expires_at = ?",
                (time.time() - 7200, time.time() - 3600, time.time() - 3600),
            )
        report = await store.run_maintenance(
            HistorySettings(True, 10, 50, True),
            preview_max_edge=320,
            preview_quality=75,
        )
        assert report["repaired"]["expired_leases"] == 1
        assert report["stats"]["active_leases"] == 0
        with store._connect() as conn:
            access = conn.execute("SELECT * FROM agent_asset_leases").fetchone()
            assert access["asset_id"] == asset_id
            assert access["expires_at"] == access["hard_expires_at"] == 0
            assert conn.execute("PRAGMA foreign_key_check").fetchall() == []

        repeated = await store.run_maintenance(
            HistorySettings(True, 10, 50, True),
            preview_max_edge=320,
            preview_quality=75,
        )
        assert repeated["repaired"]["expired_leases"] == 0
        restarted = GenerationStore(tmp_path)
        await restarted.initialize()
        assert await restarted.generation_detail(generation_id) is not None
        lookup = {"detail": "original", "preview_max_edge": 320, "preview_quality": 75}
        denied = await restarted.load_workflow_image_detailed(
            asset_id, scope_id="test:private:user-2", **lookup
        )
        assert denied.status == "access_denied"
        # A new task uses the same conversation scope, not the previous task instance.
        loaded = await restarted.load_workflow_image_detailed(
            asset_id, scope_id="test:private:user-1", **lookup
        )
        assert loaded.status == "ok"
        assert loaded.image.data == PNG
        with restarted._connect() as conn:
            access = conn.execute("SELECT * FROM agent_asset_leases").fetchone()
            assert (
                access["expires_at"]
                == access["hard_expires_at"]
                == access["last_accessed_at"] + 3600
            )
            assert access["expires_at"] > time.time()
        assert restarted._storage_stats_sync()["active_leases"] == 1

    asyncio.run(run())


def test_initialize_shortens_legacy_retention_without_revoking_or_extending_access(
    tmp_path,
) -> None:
    async def run() -> None:
        store = GenerationStore(tmp_path)
        await store.initialize()
        await store.record_success(
            provider=provider(),
            request=GenerationRequest(
                mode="text2img", provider_id="test-provider", prompt="legacy"
            ),
            images=(GeneratedImage(PNG, "image/png"),),
            elapsed_ms=10,
            history=HistorySettings(True, 10, 50, False),
        )
        now = time.time()
        cases = {
            "recent": (now - 1800, now + 23 * 3600, now + 6 * 24 * 3600),
            "old": (now - 7200, now + 22 * 3600, now + 6 * 24 * 3600),
            "shorter": (now - 1200, now + 100, now + 100),
            "released": (now - 1200, 0, 0),
        }
        for scope, values in cases.items():
            await store.lease_agent_images(
                (GeneratedImage(PNG, "image/png"),),
                scope_id=scope,
                create_preview=False,
                preview_max_edge=320,
                preview_quality=75,
            )
            with store._connect() as conn:
                conn.execute(
                    "UPDATE agent_asset_leases SET last_accessed_at = ?, expires_at = ?, hard_expires_at = ? WHERE scope_id = ?",
                    (*values, scope),
                )

        restarted = GenerationStore(tmp_path)
        await restarted.initialize()
        with restarted._connect() as conn:
            first = {
                row["scope_id"]: dict(row)
                for row in conn.execute("SELECT * FROM agent_asset_leases")
            }
        assert set(first) == set(cases)
        assert all(
            first[scope]["last_accessed_at"] == values[0]
            for scope, values in cases.items()
        )
        assert (
            first["recent"]["expires_at"]
            == first["recent"]["hard_expires_at"]
            == cases["recent"][0] + 3600
        )
        assert first["old"]["expires_at"] == first["old"]["hard_expires_at"] == 0
        assert (
            first["shorter"]["expires_at"]
            == first["shorter"]["hard_expires_at"]
            == now + 100
        )
        assert (
            first["released"]["expires_at"] == first["released"]["hard_expires_at"] == 0
        )
        await restarted.initialize()
        with restarted._connect() as conn:
            second = {
                row["scope_id"]: dict(row)
                for row in conn.execute("SELECT * FROM agent_asset_leases")
            }
        assert second == first

    asyncio.run(run())


def test_missing_archived_file_preserves_scope_for_repair_without_extending_retention(
    tmp_path,
) -> None:
    async def run() -> None:
        store = GenerationStore(tmp_path)
        await store.initialize()
        await store.record_success(
            provider=provider(),
            request=GenerationRequest(
                mode="text2img", provider_id="test-provider", prompt="repair"
            ),
            images=(GeneratedImage(PNG, "image/png"),),
            elapsed_ms=10,
            history=HistorySettings(True, 10, 50, False),
        )
        assets = await store.lease_agent_images(
            (GeneratedImage(PNG, "image/png"),),
            scope_id="test:private:user-1",
            create_preview=False,
            preview_max_edge=320,
            preview_quality=75,
        )
        asset_id = assets[0].asset_id
        original = next(store.assets_dir.rglob("*.png"))
        original.unlink()
        lookup = {"detail": "original", "preview_max_edge": 320, "preview_quality": 75}
        missing = await store.load_workflow_image_detailed(
            asset_id, scope_id="test:private:user-1", **lookup
        )
        assert missing.status == "file_missing"
        report = await store.run_maintenance(
            HistorySettings(True, 10, 50, False),
            preview_max_edge=320,
            preview_quality=75,
        )
        assert report["repaired"]["broken_assets"] == 1
        with store._connect() as conn:
            access = conn.execute("SELECT * FROM agent_asset_leases").fetchone()
            assert access["asset_id"] == asset_id
            assert access["expires_at"] == access["hard_expires_at"] == 0
        missing_again = await store.load_workflow_image_detailed(
            asset_id, scope_id="test:private:user-1", **lookup
        )
        assert missing_again.status == "file_missing"
        assert store._storage_stats_sync()["active_leases"] == 0
        original.parent.mkdir(parents=True, exist_ok=True)
        original.write_bytes(PNG)
        denied = await store.load_workflow_image_detailed(
            asset_id, scope_id="test:private:user-2", **lookup
        )
        assert denied.status == "access_denied"
        repaired = await store.load_workflow_image_detailed(
            asset_id, scope_id="test:private:user-1", **lookup
        )
        assert repaired.status == "ok"
        assert repaired.image.data == PNG

    asyncio.run(run())


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


def test_gallery_stores_and_searches_invocation_identity_snapshot(tmp_path) -> None:
    async def run() -> None:
        store = GenerationStore(tmp_path)
        await store.initialize()
        generation_id = await store.record_success(
            provider=provider(),
            request=GenerationRequest(
                mode="text2img",
                provider_id="test-provider",
                prompt="identity search",
                source="llm_tool",
                invocation_source=InvocationSource(
                    context_type="group",
                    platform_name="aiocqhttp",
                    platform_id="bot-1",
                    group_id="group-42",
                    group_name="绘图讨论组",
                    user_id="user-7",
                    user_name="空雨",
                ),
            ),
            images=(GeneratedImage(PNG, "image/png"),),
            elapsed_ms=12,
            history=HistorySettings(True, 10, 50, False, True),
        )

        detail = await store.generation_detail(generation_id)
        assert detail is not None
        assert detail["invocation_source"] == {
            "context_type": "group",
            "platform_name": "aiocqhttp",
            "platform_id": "bot-1",
            "group_id": "group-42",
            "group_name": "绘图讨论组",
            "user_id": "user-7",
            "user_name": "空雨",
        }
        for keyword in ("group-42", "绘图讨论组", "user-7", "空雨"):
            listing = await store.list_generations({"query": keyword})
            assert listing["total"] == 1
            assert listing["items"][0]["id"] == generation_id

    asyncio.run(run())


def test_gallery_does_not_store_identity_when_disabled(tmp_path) -> None:
    async def run() -> None:
        store = GenerationStore(tmp_path)
        await store.initialize()
        generation_id = await store.record_success(
            provider=provider(),
            request=GenerationRequest(
                mode="text2img",
                provider_id="test-provider",
                prompt="private identity",
                invocation_source=InvocationSource(
                    context_type="private",
                    platform_name="qq_official",
                    platform_id="bot-1",
                    user_id="user-secret",
                    user_name="用户昵称",
                ),
            ),
            images=(GeneratedImage(PNG, "image/png"),),
            elapsed_ms=12,
            history=HistorySettings(True, 10, 50, False, False),
        )

        detail = await store.generation_detail(generation_id)
        assert detail is not None
        assert not any(detail["invocation_source"].values())
        assert (await store.list_generations({"query": "user-secret"}))["total"] == 0

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


def test_gallery_filters_generations_by_invocation_source(tmp_path) -> None:
    async def run() -> None:
        store = GenerationStore(tmp_path)
        await store.initialize()
        for source in ("webui", "llm_tool"):
            await store.record_success(
                provider=provider(),
                request=GenerationRequest(
                    mode="text2img",
                    provider_id="test-provider",
                    prompt=f"source {source}",
                    source=source,
                ),
                images=(GeneratedImage(PNG, "image/png"),),
                elapsed_ms=12,
                history=HistorySettings(True, 10, 50, False),
            )

        listing = await store.list_generations({"source": "llm_tool"})

        assert listing["total"] == 1
        assert listing["items"][0]["source"] == "llm_tool"
        assert listing["items"][0]["prompt_preview"] == "source llm_tool"

    asyncio.run(run())


def test_gallery_image_sequence_and_individual_assets(tmp_path) -> None:
    async def run() -> None:
        from PIL import Image

        output = io.BytesIO()
        Image.new("RGB", (5, 3), "#4c8074").save(output, "PNG")
        wide_png = output.getvalue()
        store = GenerationStore(tmp_path)
        await store.initialize()
        older_id = await store.record_success(
            provider=provider(),
            request=GenerationRequest(
                mode="text2img",
                provider_id="test-provider",
                prompt="older sequence item",
                source="webui",
            ),
            images=(GeneratedImage(PNG, "image/png"),),
            elapsed_ms=12,
            history=HistorySettings(True, 10, 50, False),
        )
        newer_id = await store.record_success(
            provider=provider(),
            request=GenerationRequest(
                mode="img2img",
                provider_id="test-provider",
                prompt="newer sequence item",
                source="llm_tool",
            ),
            images=(
                GeneratedImage(wide_png, "image/png"),
                GeneratedImage(PNG, "image/png"),
            ),
            elapsed_ms=12,
            history=HistorySettings(True, 10, 50, False),
        )
        with store._connect() as conn:
            conn.execute(
                "UPDATE generations SET created_at = ? WHERE id = ?",
                (1_700_000_000, older_id),
            )
            conn.execute(
                "UPDATE generations SET created_at = ? WHERE id = ?",
                (1_800_000_000, newer_id),
            )

        sequence = await store.gallery_image_sequence({})
        assert [item["generation_id"] for item in sequence] == [
            newer_id,
            newer_id,
            older_id,
        ]
        assert [item["image_index"] for item in sequence] == [0, 1, 0]
        assert [item["generation_position"] for item in sequence] == [0, 0, 1]
        assert (sequence[0]["width"], sequence[0]["height"]) == (5, 3)
        assert re.fullmatch(
            r"\d{14}_i2i_test-image_01\.png",
            sequence[0]["download_filename"],
        )

        filtered = await store.gallery_image_sequence(
            {"query": "newer", "mode": "img2img", "source": "llm_tool"}
        )
        assert [item["image_id"] for item in filtered] == [
            sequence[0]["image_id"],
            sequence[1]["image_id"],
        ]
        assert await store.gallery_image_sequence({"source": "command"}) == []

        preview = await store.gallery_image_data(
            sequence[0]["image_id"], detail="preview"
        )
        original = await store.gallery_image_data(
            sequence[0]["image_id"], detail="original"
        )
        assert preview is not None and original is not None
        assert preview["data_url"].startswith("data:image/webp;base64,")
        assert original["data_url"].startswith("data:image/png;base64,")
        assert original["size_bytes"] == len(wide_png)
        assert (original["width"], original["height"]) == (5, 3)

        download = await store.gallery_image_file(sequence[0]["image_id"])
        assert download is not None
        path, mime_type, filename = download
        assert path.read_bytes() == wide_png
        assert mime_type == "image/png"
        assert filename == sequence[0]["download_filename"]
        assert await store.gallery_image_data("invalid", detail="preview") is None
        assert await store.gallery_image_file("invalid") is None

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
        assert summary["images"][0]["download_filename"] == image_name

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
        detail = await store.generation_detail(generation_id, include_assets=False)
        assert detail is not None
        assert [image["download_filename"] for image in detail["images"]] == image_names
        assert all(
            image["thumbnail_data_url"].startswith("data:image/")
            for image in detail["images"]
        )

    asyncio.run(run())
