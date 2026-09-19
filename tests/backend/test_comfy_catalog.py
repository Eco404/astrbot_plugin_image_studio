"""Shared node facts retain separate read, write and seed authorities."""

import json
import re

from astrbot_plugin_image_studio.backend.comfyui.catalog import (
    CATALOG_PATH,
    catalog_fingerprint,
    metadata_widget_catalog,
    metadata_widget_names,
    node_adapter,
    seed_policy,
)


def test_catalog_sources_are_pinned_and_layout_constraints_are_complete():
    data = json.loads(CATALOG_PATH.read_text())
    assert data["version"] == 1
    for source in data["sources"].values():
        assert re.fullmatch(r"[a-f0-9]{40}", source["commit"])
        assert source["repository"] and source["paths"]
    for adapter in data["nodes"].values():
        for source in adapter.get("sources", []):
            assert source in data["sources"]
        for layout in adapter.get("layouts", []):
            names = layout["widgets"]
            assert names and len(names) == len(set(names))
            assert adapter.get("sources")
            for index in layout.get("constraints", {}):
                assert 0 <= int(index) < len(names)
        for policy in adapter.get("frontend_inputs", {}).values():
            assert adapter.get("sources")
            assert policy["role"] == "button"
            assert policy["value_types"] == ["empty_string"]
        if "api_value_types" in adapter.get("display", {}):
            assert adapter.get("sources")
            assert adapter["display"]["api_field"]
            assert set(adapter["display"]["api_value_types"]) <= {
                "string",
                "string_list",
            }


def test_read_hints_are_not_permission_to_write_unknown_layouts():
    assert metadata_widget_names("String") == ("text",)
    assert node_adapter("String")["layouts"] == []
    assert metadata_widget_names("Unknown") == ()
    assert node_adapter("Unknown") == seed_policy("Unknown") == {}


def test_catalog_helpers_return_defensive_copies():
    adapter = node_adapter("KSampler")
    adapter["layouts"][0]["widgets"][0] = "corrupt"
    hints = metadata_widget_catalog()
    hints["KSampler"][0] = "corrupt"
    policy = seed_policy("Seed (rgthree)")
    policy["negative_sentinels"].clear()
    assert node_adapter("KSampler")["layouts"][0]["widgets"][0] == "seed"
    assert metadata_widget_names("KSampler")[0] == "seed"
    assert seed_policy("Seed (rgthree)")["negative_sentinels"] == [-1, -2, -3]
    assert re.fullmatch(r"[a-f0-9]{64}", catalog_fingerprint())


def test_seed_policies_distinguish_sentinels_plain_integers_and_frontend_controls():
    assert seed_policy("KSampler")["max"] == (1 << 64) - 1
    assert seed_policy("KSampler")["negative_sentinels"] == []
    assert seed_policy("Seed (rgthree)")["writeback"] == "rgthree"
    assert seed_policy("PrimitiveInt")["role"] == "integer"
    assert seed_policy("PrimitiveInt")["min"] < -1
    assert seed_policy("PrimitiveInt")["negative_sentinels"] == []
    assert seed_policy("SeedNode")["min"] == 0
    assert seed_policy("easy globalSeed") == {}
