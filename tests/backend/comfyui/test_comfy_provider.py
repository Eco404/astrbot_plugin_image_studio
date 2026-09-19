from __future__ import annotations

import asyncio
import copy
import io
import json
from dataclasses import replace
from types import SimpleNamespace

import aiohttp
import pytest
from astrbot_plugin_image_studio.backend.providers.comfyui.client import (
    ComfyClient,
    ComfyExecutionError,
)
from astrbot_plugin_image_studio.backend.providers.comfyui.workflows import (
    inspect_workflow,
    normalize_workflow,
    prepare_graph,
)
from astrbot_plugin_image_studio.backend.models import (
    GenerationRequest,
    ImageProvider,
    ReferenceImage,
)
from PIL import Image


def graph():
    return {
        "1": {
            "class_type": "CheckpointLoaderSimple",
            "inputs": {"ckpt_name": "model.safetensors"},
        },
        "2": {
            "class_type": "CLIPTextEncode",
            "inputs": {"text": "a cat", "clip": ["1", 1]},
        },
        "3": {
            "class_type": "EmptyLatentImage",
            "inputs": {"width": 1024, "height": 768, "batch_size": 2},
        },
        "4": {
            "class_type": "KSampler",
            "inputs": {"seed": 7, "positive": ["2", 0], "latent_image": ["3", 0]},
        },
        "5": {
            "class_type": "SaveImage",
            "inputs": {"images": ["4", 0], "filename_prefix": "ComfyUI"},
        },
    }


def config(bindings=None, outputs=None):
    return {
        "api_graph": graph(),
        "workflow": {"nodes": [], "links": []},
        "bindings": bindings or {},
        "outputs": ["5"] if outputs is None else outputs,
    }


def binding(node_id, input_name, *, source="parameter", kind="text", **extra):
    return {
        "node_id": node_id,
        "input_name": input_name,
        "source": source,
        "type": kind,
        **extra,
    }


def request(**changes):
    return replace(
        GenerationRequest(
            mode="text2img", provider_id="comfy", model="workflow", prompt="a dog"
        ),
        **changes,
    )


def provider(**changes):
    return SimpleNamespace(
        **{
            "base_url": "https://comfy.invalid/prefix",
            "timeout_seconds": 15,
            "api_key": "test-secret",
            "custom_headers": "",
            "proxy": "http://proxy.invalid:7890",
            **changes,
        }
    )


def definitions():
    return {
        "CheckpointLoaderSimple": {
            "input": {
                "required": {"ckpt_name": [["model.safetensors", "other.safetensors"]]}
            },
            "output": ["MODEL", "CLIP", "VAE"],
        },
        "CLIPTextEncode": {
            "input": {"required": {"text": ["STRING"], "clip": ["CLIP"]}},
            "output": ["CONDITIONING"],
        },
        "EmptyLatentImage": {
            "input": {
                "required": {
                    "width": ["INT", {"min": 64, "max": 4096}],
                    "height": ["INT"],
                    "batch_size": ["INT"],
                }
            },
            "output": ["LATENT"],
        },
        "KSampler": {
            "input": {
                "required": {
                    "seed": ["INT"],
                    "positive": ["CONDITIONING"],
                    "latent_image": ["LATENT"],
                }
            },
            "output": ["IMAGE"],
        },
        "SaveImage": {
            "input": {"required": {"images": ["IMAGE"], "filename_prefix": ["STRING"]}},
            "output": [],
            "output_node": True,
        },
        "LoadImage": {
            "input": {
                "required": {"image": [["server-existing.png"], {"image_upload": True}]}
            },
            "output": ["IMAGE", "MASK"],
        },
    }


class Stream:
    def __init__(self, raw):
        self.raw = raw

    async def iter_chunked(self, size):
        for start in range(0, len(self.raw), size):
            yield self.raw[start : start + size]


class Response:
    def __init__(self, payload=None, *, raw=None, status=200):
        self.status = status
        self.content = Stream(
            json.dumps(payload or {}).encode() if raw is None else raw
        )

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return None


class Session:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        result = self.responses.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result


def png():
    output = io.BytesIO()
    Image.new("RGB", (32, 32), "red").save(output, "PNG")
    return output.getvalue()


def history(*names, status=None, node="5"):
    return {
        "status": status or {"status_str": "success", "completed": True},
        "outputs": {
            node: {
                "images": [
                    {"filename": name, "subfolder": "a", "type": "output"}
                    for name in names
                ]
            }
        },
    }


@pytest.mark.parametrize(
    "wrap",
    [
        lambda value: value,
        lambda value: json.dumps(value),
        lambda value: {"prompt": json.dumps(value), "workflow": {"nodes": []}},
        lambda value: {"image_metadata": {"raw": {"prompt": value}}},
    ],
)
def test_import_api_graph_from_supported_envelopes(wrap):
    result = normalize_workflow(wrap(graph()))
    assert result["api_graph"] == graph()
    assert len(result["fingerprint"]) == 64


def test_ui_workflow_is_not_silently_converted():
    with pytest.raises(ValueError, match="界面工作流"):
        normalize_workflow({"workflow": {"nodes": [], "links": []}})


def test_binding_never_replaces_conditioning_links_or_wrong_node_type():
    with pytest.raises(ValueError, match="连线"):
        normalize_workflow(config({"prompt": binding("4", "positive")}))
    stale = binding("2", "text")
    stale["targets"] = [
        {"node_id": "2", "input_name": "text", "class_type": "DifferentNode"}
    ]
    with pytest.raises(ValueError, match="类型已"):
        normalize_workflow(config({"prompt": stale}))


def test_duplicate_bindings_rejected():
    with pytest.raises(ValueError, match="重复绑定"):
        normalize_workflow(
            config({"a": binding("2", "text"), "b": binding("2", "text")})
        )


def test_preparation_preserves_unknown_nodes_links_original_and_native_batch():
    value = config({"positive": binding("2", "text", source="prompt")})
    value["api_graph"]["99"] = {
        "class_type": "UnknownDebugNode",
        "inputs": {"anything": "keep me"},
    }
    before = copy.deepcopy(value)
    result = prepare_graph(value, request())
    assert result["2"]["inputs"]["text"] == "a dog"
    assert result["4"]["inputs"]["positive"] == ["2", 0]
    assert result["3"]["inputs"]["batch_size"] == 2
    assert result["99"] == value["api_graph"]["99"]
    assert value == before


def test_one_parameter_fans_out_to_explicit_targets_with_large_seed_preserved():
    value = config(
        {
            "seed": {
                "targets": [
                    {"node_id": "4", "input_name": "seed"},
                    {"node_id": "3", "input_name": "batch_size"},
                ],
                "source": "seed",
                "type": "number",
            }
        }
    )
    seed = 2**63 - 1
    result = prepare_graph(value, request(parameters={"seed": str(seed)}))
    assert result["4"]["inputs"]["seed"] == result["3"]["inputs"]["batch_size"] == seed


@pytest.mark.parametrize("canonical", [False, True])
def test_execution_clears_old_fingerprints_but_preserves_seed_inputs_and_archive(
    canonical,
):
    value = config()
    value["api_graph"]["91"] = {
        "class_type": "Seed (rgthree)",
        "inputs": {"seed": -1},
        "is_changed": [862058598661582],
    }
    value["api_graph"]["4"]["inputs"]["seed"] = ["91", 0]
    value["api_graph"]["99"] = {
        "class_type": "CustomNode",
        "inputs": {"is_changed": "legitimate node input"},
        "is_changed": False,
        "_meta": {"is_changed": "keep metadata"},
    }
    if canonical:
        value["api_graph_json"] = json.dumps(value["api_graph"])
    original = copy.deepcopy(value)
    normalized = normalize_workflow(value)
    assert normalized["api_graph"]["91"]["is_changed"] == [862058598661582]
    for _ in range(2):
        prepared = prepare_graph(value, request())
        assert all("is_changed" not in node for node in prepared.values())
        assert prepared["91"]["inputs"]["seed"] == -1
        assert prepared["4"]["inputs"]["seed"] == ["91", 0]
        assert prepared["99"]["inputs"]["is_changed"] == "legitimate node input"
        assert prepared["99"]["_meta"]["is_changed"] == "keep metadata"
    assert value == original


def test_width_height_count_only_change_when_explicitly_bound():
    value = config(
        {
            "width": binding("3", "width", source="width", kind="number"),
            "count": binding("3", "batch_size", source="count", kind="number"),
        }
    )
    result = prepare_graph(value, request(size="1280x960", count=5))
    assert result["3"]["inputs"] == {"width": 1280, "height": 768, "batch_size": 5}


def test_optional_parameter_uses_graph_literal_required_input_is_actionable():
    value = config({"positive": binding("2", "text")})
    assert prepare_graph(value, request())["2"]["inputs"]["text"] == "a cat"
    value["bindings"]["positive"]["required"] = True
    with pytest.raises(ValueError, match="positive"):
        prepare_graph(value, request())


def test_reference_order_duplicates_and_unused_inputs():
    value = config()
    value["api_graph"]["8"] = {
        "class_type": "LoadImage",
        "inputs": {"image": "old.png"},
    }
    value["api_graph"]["9"] = {
        "class_type": "LoadImage",
        "inputs": {"image": "old.png"},
    }
    value["bindings"] = {
        "base": binding(
            "8", "image", source="reference", kind="image", reference_index=0
        ),
        "mask": binding(
            "9", "image", source="reference", kind="mask", reference_index=1
        ),
    }
    result = prepare_graph(value, request(), ["uploaded.png", "uploaded.png"])
    assert (
        result["8"]["inputs"]["image"]
        == result["9"]["inputs"]["image"]
        == "uploaded.png"
    )
    with pytest.raises(ValueError, match="3"):
        prepare_graph(value, request(), ["one", "two", "three"])


def test_inspection_suggestions_exclude_disconnected_debug_branch():
    value = config()
    value["api_graph"]["99"] = {
        "class_type": "CLIPTextEncode",
        "inputs": {"text": "debug", "clip": ["1", 1]},
    }
    result = inspect_workflow(value)
    assert not any(key.startswith("node_99_") for key in result["suggested_bindings"])
    assert any(node["id"] == "99" for node in result["nodes"])


def test_preflight_checks_disconnected_missing_nodes_and_all_model_enums():
    value = config()
    value["api_graph"]["1"]["inputs"]["ckpt_name"] = "missing.safetensors"
    value["api_graph"]["99"] = {"class_type": "MissingThirdParty", "inputs": {}}
    session = Session([Response(definitions())])
    report = asyncio.run(ComfyClient(session).inspect(provider(), value))
    assert report["status"] == "blocked"
    assert {item["code"] for item in report["issues"]} == {
        "missing_node_type",
        "missing_model",
    }
    assert report["models"][0]["options"] == ["model.safetensors", "other.safetensors"]
    assert session.calls[0][2]["headers"]["Authorization"] == "Bearer test-secret"
    assert session.calls[0][2]["proxy"] == "http://proxy.invalid:7890"
    assert not session.calls[0][2]["allow_redirects"]


def test_preflight_deferred_reference_does_not_require_old_server_image():
    value = config()
    value["api_graph"]["8"] = {
        "class_type": "LoadImage",
        "inputs": {"image": "deleted.png"},
    }
    value["bindings"]["ref"] = binding("8", "image", kind="image")
    value["workflow"]["nodes"] = [
        {"id": 8, "type": "LoadImage", "widgets_values": ["deleted.png", "image"]}
    ]
    report = asyncio.run(
        ComfyClient(Session([Response(definitions())])).inspect(provider(), value)
    )
    assert report["status"] == "ready"
    value["bindings"] = {}
    report = asyncio.run(
        ComfyClient(Session([Response(definitions())])).inspect(provider(), value)
    )
    assert any(
        issue["node_id"] == "8" and issue["code"] == "value_not_in_list"
        for issue in report["issues"]
    )


def test_preflight_required_range_links_and_cycles():
    value = config()
    del value["api_graph"]["3"]["inputs"]["height"]
    value["api_graph"]["3"]["inputs"]["width"] = 12
    value["api_graph"]["4"]["inputs"]["positive"] = ["missing", 0]
    value["api_graph"]["2"]["inputs"]["clip"] = ["2", 0]
    report = asyncio.run(
        ComfyClient(Session([Response(definitions())])).inspect(provider(), value)
    )
    codes = {item["code"] for item in report["issues"]}
    assert {
        "required_input_missing",
        "input_out_of_range",
        "missing_link_node",
        "cyclic_graph",
    } <= codes


@pytest.mark.parametrize("source", ["seed", "parameter", None])
def test_template_seed_sentinel_is_valid_only_for_explicit_random_binding(source):
    value = config()
    value["api_graph"]["4"]["inputs"]["seed"] = -1
    if source:
        value["bindings"]["rng"] = binding(
            "4", "seed", source=source, kind="number", min=0, max=100
        )
    object_info = definitions()
    object_info["KSampler"]["input"]["required"]["seed"] = [
        "INT",
        {"min": 0, "max": 100},
    ]
    report = asyncio.run(
        ComfyClient(Session([Response(object_info)])).inspect(provider(), value)
    )
    seed_errors = [
        item
        for item in report["issues"]
        if item.get("node_id") == "4" and item.get("input_name") == "seed"
    ]
    assert bool(seed_errors) is (source != "seed")


def test_unknown_custom_literals_warn_instead_of_guessing_function():
    value = config()
    value["api_graph"]["2"]["inputs"]["dynamic_extra"] = "text"
    report = asyncio.run(
        ComfyClient(Session([Response(definitions())])).inspect(provider(), value)
    )
    assert report["status"] == "warning"
    assert report["issues"][0]["code"] == "unknown_input"


def auxiliary_config(kind, name, payload):
    value = config()
    value["api_graph"]["9"] = {"class_type": kind, "inputs": {name: payload}}
    info = definitions()
    info[kind] = {"input": {"required": {}, "optional": {}}, "output": ["*"]}
    return value, info


@pytest.mark.parametrize(
    "kind,name,payload",
    [
        ("easy showAnything", "text", "previous display snapshot"),
        ("easy showAnything", "text", ["one", "two"]),
        ("easy showAnything", "text", []),
        ("WeiLinPromptUI", "打开提示词编辑器", ""),
        ("WeiLinPromptUI", "打开Lora堆", ""),
        ("WeiLinPromptUI", "Open Prompt UI", ""),
        ("WeiLinPromptUI", "Open Lora Stack", ""),
    ],
)
def test_known_auxiliary_literals_pass_preflight_without_removing_data(
    kind, name, payload
):
    value, info = auxiliary_config(kind, name, payload)
    value["workflow"] = {
        "nodes": [{"id": 9, "type": kind, "widgets_values": [payload]}],
        "links": [],
    }
    before = copy.deepcopy(value)
    report = asyncio.run(
        ComfyClient(Session([Response(info)])).inspect(provider(), value)
    )
    assert report["status"] == "ready"
    assert report["issues"] == []
    assert value == before
    node = next(node for node in report["nodes"] if node["id"] == "9")
    assert node["inputs"][0]["value"] == payload


@pytest.mark.parametrize(
    "kind,name,payload",
    [
        ("easy showAnything", "text", {"unexpected": "shape"}),
        ("easy showAnything", "text", ["text", None]),
        ("easy showAnything", "text", 12),
        ("WeiLinPromptUI", "打开提示词编辑器", "unexpected button value"),
        ("WeiLinPromptUI", "打开Lora堆", False),
        ("WeiLinPromptUI", "打开Lora堆", None),
        ("OtherDisplay", "text", "looks like a display"),
        ("easy showAnything", "other_text", "not the verified field"),
        ("WeiLinPromptUI", "another_button", None),
    ],
)
def test_auxiliary_exemptions_do_not_hide_unknown_fields_or_shapes(kind, name, payload):
    value, info = auxiliary_config(kind, name, payload)
    report = asyncio.run(
        ComfyClient(Session([Response(info)])).inspect(provider(), value)
    )
    assert report["status"] == "warning"
    assert report["issues"][0]["code"] == "unknown_input"


@pytest.mark.parametrize(
    "kind,name", [("easy showAnything", "text"), ("WeiLinPromptUI", "打开Lora堆")]
)
@pytest.mark.parametrize("upstream", ["2", "missing"])
def test_auxiliary_links_remain_visible_even_for_a_missing_source(kind, name, upstream):
    value, info = auxiliary_config(kind, name, [upstream, 0])
    report = asyncio.run(
        ComfyClient(Session([Response(info)])).inspect(provider(), value)
    )
    assert report["status"] == "warning"
    assert report["issues"][0]["code"] == "auxiliary_input_link"
    assert "连线" in report["issues"][0]["message"]


@pytest.mark.parametrize(
    "kind,name,payload",
    [("easy showAnything", "text", "cached"), ("WeiLinPromptUI", "打开Lora堆", "")],
)
@pytest.mark.parametrize("edit", ["binding", "fixed", "persisted_fixed"])
def test_auxiliary_bindings_and_fixed_edits_are_not_silenced(kind, name, payload, edit):
    value, info = auxiliary_config(kind, name, payload)
    if edit == "binding":
        value["bindings"] = {"bad_target": binding("9", name)}
    else:
        value["input_overrides"] = [
            {"node_id": "9", "input_name": name, "value": payload}
        ]
        if edit == "persisted_fixed":
            value = normalize_workflow(value)
    report = asyncio.run(
        ComfyClient(Session([Response(info)])).inspect(provider(), value)
    )
    issues = [item for item in report["issues"] if item["node_id"] == "9"]
    assert issues[0]["code"] == "auxiliary_input_edit"
    assert "实际执行输入" in issues[0]["message"]


def test_remote_declaration_wins_over_auxiliary_catalog():
    value, info = auxiliary_config("easy showAnything", "text", "not in choices")
    info["easy showAnything"]["input"]["optional"]["text"] = [["valid"]]
    report = asyncio.run(
        ComfyClient(Session([Response(info)])).inspect(provider(), value)
    )
    assert report["issues"][0]["code"] == "value_not_in_list"
    assert report["status"] == "blocked"


def test_auxiliary_literals_do_not_hide_required_inputs_models_or_missing_nodes():
    value, info = auxiliary_config("easy showAnything", "text", "snapshot")
    info["easy showAnything"]["input"]["required"]["anything"] = ["*"]
    value["api_graph"]["1"]["inputs"]["ckpt_name"] = "missing.safetensors"
    value["api_graph"]["10"] = {"class_type": "Absent", "inputs": {}}
    report = asyncio.run(
        ComfyClient(Session([Response(info)])).inspect(provider(), value)
    )
    assert report["status"] == "blocked"
    assert {item["code"] for item in report["issues"]} == {
        "required_input_missing",
        "missing_model",
        "missing_node_type",
    }


def test_submit_records_api_and_ui_graph_without_mutation_and_keeps_partial_errors():
    errors = {"6": {"class_type": "SaveImage", "errors": [{"message": "failed"}]}}
    session = Session([Response({"prompt_id": "remote-id", "node_errors": errors})])
    value = config()
    result = asyncio.run(
        ComfyClient(session).submit(
            provider(), value["api_graph"], value["workflow"], client_id="client"
        )
    )
    payload = session.calls[0][2]["json"]
    assert payload["prompt"] == value["api_graph"]
    assert payload["extra_data"]["extra_pnginfo"]["workflow"] == value["workflow"]
    assert result["node_errors"] == errors


def test_direct_submission_drops_old_fingerprints_on_every_request_without_mutation():
    async def run():
        session = Session(
            [Response({"prompt_id": "one"}), Response({"prompt_id": "two"})]
        )
        client = ComfyClient(session)
        value = config()
        value["api_graph"]["91"] = {
            "class_type": "Seed (rgthree)",
            "inputs": {"seed": -1},
            "is_changed": [862058598661582],
        }
        value["api_graph"]["4"]["inputs"]["seed"] = ["91", 0]
        original = copy.deepcopy(value)
        for _ in range(2):
            await client.submit(provider(), value["api_graph"], value["workflow"])
            payload = session.calls[-1][2]["json"]
            assert "is_changed" not in payload["prompt"]["91"]
            assert payload["prompt"]["91"]["inputs"]["seed"] == -1
            assert payload["prompt"]["4"]["inputs"]["seed"] == ["91", 0]
            # ComfyUI may add a new fingerprint to the submitted graph while
            # executing. Neither this nor the old archive can seed a new request.
            payload["prompt"]["91"]["is_changed"] = [123456]
        assert value == original

    asyncio.run(run())


def test_uncertain_submission_never_retries_and_redacts_transport_credentials():
    session = Session(
        [aiohttp.ClientConnectionError("Bearer SECRET https://secret.invalid")]
    )
    with pytest.raises(ComfyExecutionError) as error:
        asyncio.run(ComfyClient(session).submit(provider(), graph()))
    assert error.value.unknown_submission
    assert "SECRET" not in str(error.value)
    assert len(session.calls) == 1


def test_rejected_submission_has_actionable_node_field_without_unknown_flag():
    session = Session(
        [
            Response(
                {
                    "node_errors": {
                        "1": {
                            "class_type": "CheckpointLoaderSimple",
                            "errors": [
                                {
                                    "type": "value_not_in_list",
                                    "details": "missing.safetensors not available",
                                    "extra_info": {"input_name": "ckpt_name"},
                                }
                            ],
                        }
                    }
                },
                status=400,
            )
        ]
    )
    with pytest.raises(ComfyExecutionError) as error:
        asyncio.run(ComfyClient(session).submit(provider(), graph()))
    assert "ckpt_name" in str(error.value) and "#1" in str(error.value)
    assert not error.value.unknown_submission


def test_history_polling_recovers_from_connection_loss_without_resubmission():
    session = Session(
        [
            aiohttp.ClientConnectionError(),
            Response({}),
            Response({"queue_running": [[1, "job", {}, {}]], "queue_pending": []}),
            Response({"job": history("a.png")}),
        ]
    )
    updates = []
    result = asyncio.run(
        ComfyClient(session).wait(
            provider(),
            "job",
            timeout_seconds=1,
            poll_interval=0.01,
            on_progress=updates.append,
        )
    )
    assert result["outputs"]["5"]["images"][0]["filename"] == "a.png"
    assert [item["status"] for item in updates] == ["reconnecting", "running"]
    assert all(call[0] == "GET" for call in session.calls)


def test_history_failure_retains_partial_outputs():
    entry = history(
        "a.png",
        status={
            "status_str": "error",
            "completed": False,
            "messages": [
                [
                    "execution_error",
                    {
                        "node_id": "9",
                        "node_type": "Custom",
                        "exception_message": "model missing",
                    },
                ]
            ],
        },
    )
    with pytest.raises(ComfyExecutionError) as error:
        asyncio.run(
            ComfyClient(Session([Response({"job": entry})])).wait(provider(), "job")
        )
    assert error.value.history == entry
    assert "#9" in str(error.value)


def test_all_selected_images_collected_in_order_unselected_previews_skipped():
    entry = history("a.png", "b.png")
    entry["outputs"]["9"] = {"images": [{"filename": "debug.png"}]}
    session = Session([Response(raw=png()), Response(raw=png())])
    results = asyncio.run(
        ComfyClient(session).download_outputs(provider(), entry, ["5"])
    )
    assert len(results) == 2
    assert [call[2]["params"]["filename"] for call in session.calls] == [
        "a.png",
        "b.png",
    ]
    assert all(item.mime_type == "image/png" for item in results)


def test_invalid_image_and_missing_selected_output_preserve_valid_results():
    session = Session([Response(raw=png()), Response(raw=b"not image")])
    with pytest.raises(ComfyExecutionError) as error:
        asyncio.run(
            ComfyClient(session).download_outputs(
                provider(), history("a.png", "b.png"), ["5", "9"]
            )
        )
    assert len(error.value.images) == 1
    assert len(error.value.failures) == 2
    assert "#9" in str(error.value)


def test_upload_uses_server_name_preserves_order_custom_auth_and_proxy():
    session = Session(
        [
            Response({"name": "renamed.png", "subfolder": "upload"}),
            Response({"name": "mask.png"}),
        ]
    )
    reference = ReferenceImage("asset", "source.png", png(), "image/png")
    result = asyncio.run(
        ComfyClient(session).upload_references(
            provider(
                custom_headers='{"Authorization":"Basic explicit","Content-Type":"application/json"}'
            ),
            [reference, reference],
        )
    )
    assert result == ["upload/renamed.png", "mask.png"]
    assert all(
        call[2]["headers"] == {"Authorization": "Basic explicit"}
        for call in session.calls
    )
    assert all(isinstance(call[2]["data"], aiohttp.FormData) for call in session.calls)


def test_targeted_cancel_and_old_server_fallback_never_global_interrupt():
    session = Session([Response({"cancelled": True})])
    result = asyncio.run(ComfyClient(session).cancel(provider(), "job"))
    assert result["cancelled"]
    assert session.calls[0][1].endswith("/api/jobs/job/cancel")
    session = Session([Response(status=404), Response()])
    result = asyncio.run(ComfyClient(session).cancel(provider(), "job"))
    assert result["status"] == "queued_removed_running_unchanged"
    assert session.calls[1][2]["json"] == {"delete": ["job"]}
    assert all("interrupt" not in call[1] for call in session.calls)


def test_resumed_execution_does_not_upload_or_submit_again():
    session = Session([Response({"job": history("a.png")}), Response(raw=png())])
    result = asyncio.run(
        ComfyClient(session).execute(
            provider(), request(), config=config(), resume_id="job"
        )
    )
    assert len(result) == 1
    assert all(call[0] == "GET" for call in session.calls)


def test_execute_callbacks_persist_graph_and_remote_id_before_wait():
    session = Session(
        [
            Response(definitions()),
            Response({"prompt_id": "job"}),
            Response({"job": history("a.png")}),
            Response(raw=png()),
        ]
    )
    events = []

    def prepared(value):
        assert len(session.calls) == 1
        assert value["api_graph"] == graph()
        events.append("prepared")

    async def submitted(value):
        assert len(session.calls) == 2
        assert value["prompt_id"] == "job"
        events.append("submitted")

    result = asyncio.run(
        ComfyClient(session).execute(
            provider(),
            request(),
            config=config(),
            on_prepared=prepared,
            on_submitted=submitted,
        )
    )
    assert events == ["prepared", "submitted"]
    assert len(result) == 1


def test_partial_node_errors_never_report_full_success():
    errors = {
        "9": {"class_type": "SaveImage", "errors": [{"message": "missing input"}]}
    }
    session = Session(
        [
            Response(definitions()),
            Response({"prompt_id": "job", "node_errors": errors}),
            Response({"job": history("a.png")}),
            Response(raw=png()),
        ]
    )
    with pytest.raises(ComfyExecutionError) as error:
        asyncio.run(
            ComfyClient(session).execute(provider(), request(), config=config())
        )
    assert len(error.value.images) == 1
    assert error.value.node_errors == errors


def test_model_replacement_validated_after_bound_parameters_apply():
    value = config({"checkpoint": binding("1", "ckpt_name", kind="select")})
    value["api_graph"]["1"]["inputs"]["ckpt_name"] = "missing-original.safetensors"
    session = Session(
        [
            Response(definitions()),
            Response({"prompt_id": "job"}),
            Response({"job": history("a.png")}),
            Response(raw=png()),
        ]
    )
    result = asyncio.run(
        ComfyClient(session).execute(
            provider(),
            request(parameters={"checkpoint": "other.safetensors"}),
            config=value,
        )
    )
    assert len(result) == 1
    assert (
        session.calls[1][2]["json"]["prompt"]["1"]["inputs"]["ckpt_name"]
        == "other.safetensors"
    )


def test_append_empty_prompt_preserves_original():
    value = config({"text": binding("2", "text", source="prompt", mode="append")})
    assert prepare_graph(value, request(prompt=""))["2"]["inputs"]["text"] == "a cat"


def test_mask_upload_converts_white_edit_to_native_inverse_alpha():
    value = config()
    value["api_graph"]["8"] = {
        "class_type": "LoadImage",
        "inputs": {"image": "old.png"},
    }
    value["bindings"]["mask"] = binding("8", "image", kind="mask", reference_index=0)
    source = Image.new("L", (2, 1), 0)
    source.putpixel((1, 0), 255)
    buffer = io.BytesIO()
    source.save(buffer, "PNG")
    reference = ReferenceImage("asset", "mask.png", buffer.getvalue(), "image/png")
    session = Session([Response({"name": "mask.png"})])
    asyncio.run(
        ComfyClient(session).upload_references(provider(), [reference], config=value)
    )
    form = session.calls[0][2]["data"]
    raw = next(field[2] for field in form._fields if field[0]["name"] == "image")
    with Image.open(io.BytesIO(raw)) as result:
        assert result.mode == "RGBA"
        assert result.getpixel((0, 0))[3] == 255
        assert result.getpixel((1, 0))[3] == 0


def test_canonical_graph_json_preserves_uint64_seed_through_browser_preview():
    original = graph()
    original["4"]["inputs"]["seed"] = 18446744073709551615
    rounded_preview = copy.deepcopy(original)
    rounded_preview["4"]["inputs"]["seed"] = 18446744073709552000
    value = {
        "api_graph": rounded_preview,
        "api_graph_json": json.dumps(original),
        "input_overrides": [
            {"node_id": "2", "input_name": "text", "value": "modified"}
        ],
    }
    result = normalize_workflow(value)
    assert result["api_graph"]["4"]["inputs"]["seed"] == 18446744073709551615
    assert result["api_graph"]["2"]["inputs"]["text"] == "modified"
    assert (
        json.loads(result["api_graph_json"])["4"]["inputs"]["seed"]
        == 18446744073709551615
    )
    assert "input_overrides" not in result


def test_large_integer_override_accepts_decimal_string_without_losing_precision():
    value = {
        "api_graph_json": json.dumps(graph()),
        "input_overrides": [
            {"node_id": "4", "input_name": "seed", "value": "18446744073709551615"}
        ],
    }
    assert (
        normalize_workflow(value)["api_graph"]["4"]["inputs"]["seed"]
        == 18446744073709551615
    )
    value["input_overrides"] = [
        {"node_id": "4", "input_name": "positive", "value": "literal"}
    ]
    with pytest.raises(ValueError, match="连线"):
        normalize_workflow(value)


def test_large_integer_inspection_offers_exact_text_default():
    value = config()
    value["api_graph"]["4"]["inputs"]["seed"] = 18446744073709551615
    result = inspect_workflow(value)
    assert (
        result["suggested_bindings"]["node_4_seed"]["default"] == "18446744073709551615"
    )
    item = next(node for node in result["nodes"] if node["id"] == "4")
    assert (
        next(field for field in item["inputs"] if field["name"] == "seed")["value_text"]
        == "18446744073709551615"
    )


def test_tool_seed_default_remains_exact_across_browser_round_trip():
    provider = ImageProvider.from_mapping(
        {
            "id": "comfy",
            "kind": "comfyui",
            "models": [
                {
                    "id": "workflow",
                    "comfyui": config(),
                    "parameters": {"seed": {"type": "number", "default": -1}},
                    "tool": {
                        "parameters": {
                            "seed": {
                                "exposed": True,
                                "default_override": 18446744073709551615,
                            }
                        }
                    },
                }
            ],
        }
    )
    public = provider.public_dict()
    assert (
        public["models"][0]["tool"]["parameters"]["seed"]["default_override"]
        == "18446744073709551615"
    )
    restored = ImageProvider.from_mapping(json.loads(json.dumps(public)))
    assert (
        int(restored.models[0].tool["parameters"]["seed"]["default_override"])
        == 18446744073709551615
    )


def test_websocket_reports_only_own_job_and_history_confirms_completion():
    class WebSocket:
        closed = False

        def __aiter__(self):
            async def events():
                for prompt_id in ("other-job", "", "job"):
                    yield SimpleNamespace(
                        type=aiohttp.WSMsgType.TEXT,
                        data=json.dumps(
                            {
                                "type": "progress",
                                "data": {"prompt_id": prompt_id, "value": 4, "max": 20},
                            }
                        ),
                    )

            return events()

        async def close(self):
            self.closed = True

    class WebSocketSession(Session):
        async def ws_connect(self, url, **kwargs):
            self.ws = WebSocket()
            self.ws_arguments = (url, kwargs)
            return self.ws

    session = WebSocketSession(
        [
            Response({}),
            Response({"queue_running": [[1, "job"]]}),
            Response({"job": history("a.png")}),
        ]
    )
    events = []
    asyncio.run(
        ComfyClient(session).wait(
            provider(),
            "job",
            client_id="own-client",
            poll_interval=0.01,
            on_progress=events.append,
        )
    )
    progress = [event for event in events if event.get("event") == "progress"]
    assert len(progress) == 1 and progress[0]["prompt_id"] == "job"
    assert session.ws_arguments[0] == "wss://comfy.invalid/prefix/ws"
    assert session.ws_arguments[1]["params"] == {"clientId": "own-client"}
    assert session.ws.closed


def test_distinct_multistage_dimensions_prefer_public_binding_keys():
    value = config(
        {"first_width": binding("3", "width", source="width", kind="number")}
    )
    value["api_graph"]["8"] = {
        "class_type": "EmptyLatentImage",
        "inputs": {"width": 512, "height": 512, "batch_size": 1},
    }
    value["bindings"]["second_width"] = binding(
        "8", "width", source="width", kind="number"
    )
    value["bindings"]["second_height"] = binding(
        "8", "height", source="height", kind="number"
    )
    prepared = prepare_graph(
        value,
        request(
            size="2048x1536",
            parameters={"first_width": 640, "second_width": 1280, "second_height": 960},
        ),
    )
    assert prepared["3"]["inputs"]["width"] == 640
    assert prepared["8"]["inputs"]["width"] == 1280
    assert prepared["8"]["inputs"]["height"] == 960


@pytest.mark.parametrize("key", ["_hidden", "123seed", "中文", "has space", "x" * 65])
def test_binding_keys_match_service_parameter_boundary(key):
    with pytest.raises(ValueError, match="参数键"):
        normalize_workflow(config({key: binding("2", "text")}))


def test_workflow_canvas_json_keeps_exact_widget_seeds_for_export():
    canvas = {
        "nodes": [{"id": 4, "widgets_values": [18446744073709551615]}],
        "links": [],
    }
    rounded = {
        "nodes": [{"id": 4, "widgets_values": [18446744073709552000]}],
        "links": [],
    }
    value = {**config(), "workflow": rounded, "workflow_json": json.dumps(canvas)}
    result = normalize_workflow(value)
    assert result["workflow"]["nodes"][0]["widgets_values"] == [18446744073709551615]
    assert json.loads(result["workflow_json"]) == canvas


def test_suggested_binding_keys_handle_subgraph_ids_and_long_inputs():
    value = config(outputs=[])
    value["api_graph"] = {
        "1:subgraph": {"class_type": "Custom", "inputs": {"long_" + "x" * 90: "text"}},
        "1_subgraph": {"class_type": "Custom", "inputs": {"long_" + "x" * 90: "text"}},
    }
    result = inspect_workflow(value)
    assert len(result["suggested_bindings"]) == 2
    normalized = normalize_workflow({**value, "bindings": result["suggested_bindings"]})
    assert len(normalized["bindings"]) == 2
    assert all(len(key) <= 64 for key in normalized["bindings"])
