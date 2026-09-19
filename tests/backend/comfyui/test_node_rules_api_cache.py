from __future__ import annotations

import asyncio
import io
import json

import httpx
import pytest
from PIL import Image, PngImagePlugin

from astrbot_plugin_image_studio.backend.gallery.errors import ImportEditConflictError
from astrbot_plugin_image_studio.backend.gallery.store import GenerationStore
from astrbot_plugin_image_studio.backend.media.files import _atomic_write
from astrbot_plugin_image_studio.backend.metadata.comfyui.user_rules import (
    get_rules,
    load_rules,
    make_rules,
    prepare_rule,
    use_rules,
)
from astrbot_plugin_image_studio.backend.metadata.parser import parse_metadata_fields
from astrbot_plugin_image_studio.tests.backend.gallery.test_gallery_api import PREFIX
from astrbot_plugin_image_studio.tests.support.webui_harness import create_app


def fixture():
    graph = {
        "1": {"class_type": "CustomTextForBinding", "inputs": {"text": "sunrise"}},
        "2": {"class_type": "CLIPTextEncode", "inputs": {"text": ["1", 0]}},
        "3": {"class_type": "CLIPTextEncode", "inputs": {"text": "blur"}},
        "4": {
            "class_type": "KSampler",
            "inputs": {"positive": ["2", 0], "negative": ["3", 0], "seed": 1},
        },
        "5": {"class_type": "VAEDecode", "inputs": {"samples": ["4", 0]}},
        "6": {"class_type": "SaveImage", "inputs": {"images": ["5", 0]}},
    }
    workflow = {
        "id": "api-cache-test",
        "nodes": [
            {
                "id": 1,
                "type": "CustomTextForBinding",
                "inputs": [{"name": "text", "type": "STRING", "link": None}],
                "outputs": [{"name": "text", "type": "STRING", "links": [1]}],
                "widgets_values": ["sunrise"],
            }
        ],
        "links": [[1, 1, 0, 2, 0, "STRING"]],
    }
    raw = {"prompt": json.dumps(graph), "workflow": json.dumps(workflow)}
    body = {
        "metadata": raw,
        "width": 24,
        "height": 32,
        "node_id": "1",
        "rule": {
            "scope": "workflow",
            "operation": "literal",
            "inputs": ["text"],
            "output_port": 0,
        },
    }
    info = PngImagePlugin.PngInfo()
    for key, value in raw.items():
        info.add_text(key, value)
    output = io.BytesIO()
    Image.new("RGB", (24, 32), "red").save(output, "PNG", pnginfo=info)
    return body, output.getvalue()


def write_rules(path, rules):
    _atomic_write(
        path / "comfyui_parser_rules.json",
        json.dumps({"version": 1, "rules": rules}).encode(),
    )


def test_rule_api_preview_save_conflict_delete_and_validation(tmp_path):
    async def run():
        app = await create_app(tmp_path, seed=False)
        body, _ = fixture()
        rule_file = app.state.plugin.store.data_dir / "comfyui_parser_rules.json"
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            listed = await client.get(PREFIX + "imports/node-rules")
            assert listed.status_code == 200
            initial = listed.json()["revision"]
            preview = await client.post(
                PREFIX + "imports/node-rules/preview", json=body
            )
            assert preview.status_code == 200, preview.text
            proposal = preview.json()
            assert proposal["parsed"]["normalized"]["prompt"] == "sunrise"
            assert proposal["revision"] == initial
            assert not rule_file.exists()
            saved = await client.post(
                PREFIX + "imports/node-rules/save", json={**body, "revision": initial}
            )
            assert saved.status_code == 200, saved.text
            assert saved.json()["revision"] != initial
            assert saved.json()["rule"]["origin"] == "user"
            stale = await client.post(
                PREFIX + "imports/node-rules/save", json={**body, "revision": initial}
            )
            assert stale.status_code == 409
            inspected = await client.post(PREFIX + "imports/inspect", json=body)
            assert inspected.json()["normalized"]["prompt"] == "sunrise"
            bad = await client.post(
                PREFIX + "imports/node-rules/preview",
                json={**body, "rule": {**body["rule"], "role": "positive"}},
            )
            assert bad.status_code == 400
            deleted = await client.post(
                PREFIX + "imports/node-rules/delete",
                json={
                    "revision": saved.json()["revision"],
                    "rule_id": saved.json()["rule"]["id"],
                },
            )
            assert deleted.status_code == 200, deleted.text
            assert deleted.json()["parsed"] is None
            assert deleted.json()["revision"] == initial
            assert json.loads(rule_file.read_text())["rules"] == []

    asyncio.run(run())


@pytest.mark.parametrize("wrong_output", ["", "16"])
def test_rule_api_requires_the_selected_save_branch_but_deletion_does_not(
    tmp_path, wrong_output
):
    async def run():
        app = await create_app(tmp_path, seed=False)
        body, _ = fixture()
        graph = json.loads(body["metadata"]["prompt"])
        graph.update(
            {
                "11": {"class_type": "CLIPTextEncode", "inputs": {"text": "other"}},
                "14": {
                    "class_type": "KSampler",
                    "inputs": {"positive": ["11", 0], "seed": 2},
                },
                "15": {"class_type": "VAEDecode", "inputs": {"samples": ["14", 0]}},
                "16": {"class_type": "SaveImage", "inputs": {"images": ["15", 0]}},
            }
        )
        body["metadata"]["prompt"] = json.dumps(graph)
        rule_file = app.state.plugin.store.data_dir / "comfyui_parser_rules.json"
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            revision = (await client.get(PREFIX + "imports/node-rules")).json()[
                "revision"
            ]
            for endpoint in ("preview", "save"):
                rejected = await client.post(
                    PREFIX + "imports/node-rules/" + endpoint,
                    json={**body, "output_node_id": wrong_output, "revision": revision},
                )
                assert rejected.status_code == 400, rejected.text
                assert "当前选定的保存分支" in rejected.text
                assert not rule_file.exists()
            saved = await client.post(
                PREFIX + "imports/node-rules/save",
                json={**body, "output_node_id": "6", "revision": revision},
            )
            assert saved.status_code == 200, saved.text
            deleted = await client.post(
                PREFIX + "imports/node-rules/delete",
                json={
                    "metadata": body["metadata"],
                    "output_node_id": wrong_output,
                    "revision": saved.json()["revision"],
                    "rule_id": saved.json()["rule"]["id"],
                },
            )
            assert deleted.status_code == 200, deleted.text
            assert json.loads(rule_file.read_text())["rules"] == []

    asyncio.run(run())


@pytest.mark.parametrize("manual", [None, "my prompt", ""])
def test_rule_revision_refreshes_reads_and_cache_preserving_manual_values(
    tmp_path, manual
):
    async def run():
        body, data = fixture()
        store = GenerationStore(tmp_path)
        await store.initialize()
        imported = await store.import_image(
            data, "custom.png", {} if manual is None else {"prompt": manual}
        )
        generation_id = imported["generation_id"]
        before = await store.import_edit_snapshot(generation_id)
        old_revision = await store.gallery_revision()
        rule = prepare_rule(body["metadata"], "1", body["rule"])
        write_rules(tmp_path, [rule])
        assert await store.gallery_revision() != old_revision
        detail = await store.generation_detail(generation_id, include_assets=False)
        image = detail["images"][0]
        assert image["metadata"]["normalized"]["prompt"] == "sunrise"
        assert image["supplemental"]["prompt"] == (
            "sunrise" if manual is None else manual
        )
        after = await store.import_edit_snapshot(generation_id)
        assert before["revision"] != after["revision"]
        assert after["items"][0]["fields"]["prompt"] == (
            "sunrise" if manual is None else manual
        )
        with pytest.raises(ImportEditConflictError):
            await store.edit_import(
                generation_id,
                before["revision"],
                [{"image_id": image["id"], "overrides": {"prompt": "stale"}}],
            )
        await asyncio.to_thread(store.metadata_records.backfill_metadata)
        with store._connect() as conn:
            saved = json.loads(
                conn.execute("SELECT metadata_json FROM image_metadata").fetchone()[0]
            )
        assert saved["rules_fingerprint"] == load_rules(tmp_path).fingerprint
        assert saved["normalized"]["prompt"] == "sunrise"
        write_rules(tmp_path, [])
        reverted = await store.generation_detail(generation_id, include_assets=False)
        assert not reverted["images"][0]["metadata"]["normalized"].get("prompt")
        assert reverted["images"][0]["supplemental"]["prompt"] == (
            "" if manual is None else manual
        )
        await store.close()

    asyncio.run(run())


def test_store_rules_are_isolated_across_worker_threads_and_projection_cache(tmp_path):
    async def run():
        body, data = fixture()
        rule = prepare_rule(body["metadata"], "1", body["rule"])
        stores = [GenerationStore(tmp_path / name) for name in ("bound", "plain")]
        write_rules(stores[0].data_dir, [rule])
        await asyncio.gather(*(store.initialize() for store in stores))
        imported = await asyncio.gather(
            *(store.import_image(data, "custom.png", {}) for store in stores)
        )
        details = await asyncio.gather(
            *(
                store.generation_detail(item["generation_id"], include_assets=False)
                for store, item in zip(stores, imported, strict=True)
            )
        )
        assert details[0]["images"][0]["metadata"]["normalized"]["prompt"] == "sunrise"
        assert not details[1]["images"][0]["metadata"]["normalized"].get("prompt")
        assert get_rules().user_rules == []
        metadata = parse_metadata_fields(body["metadata"])
        from astrbot_plugin_image_studio.backend.gallery.projection import (
            project_import_metadata,
        )

        with use_rules(make_rules([rule])):
            parsed = project_import_metadata(metadata, {"comfy_output_node": "6"})
            assert parsed["normalized"]["prompt"] == "sunrise"
        parsed = project_import_metadata(metadata, {"comfy_output_node": "6"})
        assert not parsed["normalized"].get("prompt")
        await asyncio.gather(*(store.close() for store in stores))

    asyncio.run(run())
