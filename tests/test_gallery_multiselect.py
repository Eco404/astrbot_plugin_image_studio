from __future__ import annotations

import asyncio
import base64
import json

import httpx
import pytest
from astrbot_plugin_image_studio.models import GeneratedImage, GenerationRequest
from astrbot_plugin_image_studio.tests.webui_harness import create_app

PREFIX = "/astrbot_plugin_image_studio/"
PLURAL_FIELDS = {
    "provider_ids": ("provider_id", "natural", 64),
    "modes": ("mode", "text2img", 20),
    "sources": ("source", "webui", 30),
    "generation_engines": ("generation_engine", "openai_images", 80),
}
LITERAL_PROVIDER_ID = "vendor,\" &+中') OR 1=1 --"
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Y9JZq4AAAAASUVORK5CYII="
)
# Oldest first; two records contain multiple images to distinguish image counts
# from generation counts and verify cursors across gallery pages.
RECORDS = [
    ("a", "natural", "text2img", "webui", "openai_images", True, "orchid alpha", 2),
    ("b", "nai", "text2img", "command", "novelai", True, "orchid beta", 1),
    ("c", "nai", "img2img", "llm_tool", "nai", False, "orchid gamma", 2),
    ("d", "", "unknown", "import", "unknown", True, "orchid delta", 1),
    (
        "e",
        LITERAL_PROVIDER_ID,
        "text2img",
        "import",
        "comfyui",
        True,
        "orchid epsilon",
        1,
    ),
    ("f", "natural", "img2img", "llm_tool", "openai_images", True, "forest zeta", 1),
    ("g", "natural", "text2img", "command", "openai_images", True, "orchid eta", 1),
    ("h", "", "text2img", "import", "novelai", False, "orchid theta", 1),
]


@pytest.fixture
def gallery(tmp_path):
    async def seed():
        app = await create_app(tmp_path, seed=False)
        plugin = app.state.plugin
        provider = plugin._settings.providers[0]
        ids = {}
        for index, record in enumerate(RECORDS):
            key, provider_id, mode, source, engine, favorite, prompt, count = record
            generation_id = await plugin.store.record_success(
                provider=provider,
                request=GenerationRequest(
                    mode=mode,
                    provider_id=provider.id,
                    model=provider.models[0].id,
                    prompt=prompt,
                    source=source,
                    count=count,
                ),
                images=tuple(GeneratedImage(PNG, "image/png") for _ in range(count)),
                elapsed_ms=12,
                history=plugin._settings.history,
            )
            # Model imported/unassigned records and historical engine aliases in
            # this temporary store without depending on the import upload flow.
            with plugin.store._connect() as conn:
                conn.execute(
                    "UPDATE generations SET created_at = ?, provider_id = ?, "
                    "generation_engine = ?, is_favorite = ? WHERE id = ?",
                    (
                        1_800_000_000 + index,
                        provider_id,
                        engine,
                        favorite,
                        generation_id,
                    ),
                )
            ids[key] = generation_id
        return app, ids

    return asyncio.run(seed())


async def assert_selection(gallery, filters, expected_keys):
    app, ids = gallery
    store = app.state.plugin.store
    expected_ids = [ids[key] for key in expected_keys]
    image_counts = {row[0]: row[-1] for row in RECORDS}
    expected_sequence = [
        ids[key] for key in expected_keys for _ in range(image_counts[key])
    ]
    offset, limit = filters.get("offset", 0), filters.get("limit", 24)
    expected_page = expected_ids[offset : offset + limit]

    listing = await store.list_generations(filters)
    sequence = await store.gallery_image_sequence(filters)
    assert listing["total"] == len(expected_ids)
    assert [item["id"] for item in listing["items"]] == expected_page
    assert [item["generation_id"] for item in sequence] == expected_sequence
    assert [item["generation_position"] for item in sequence] == [
        position
        for position, key in enumerate(expected_keys)
        for _ in range(image_counts[key])
    ]
    assert [item["image_index"] for item in sequence] == [
        index for key in expected_keys for index in range(image_counts[key])
    ]
    query = {
        key: json.dumps(value, ensure_ascii=False) if isinstance(value, list) else value
        for key, value in filters.items()
    }
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        list_response = await client.get(PREFIX + "gallery/list", params=query)
        sequence_response = await client.get(
            PREFIX + "gallery/image-sequence", params=query
        )
    assert list_response.status_code == 200, list_response.text
    assert sequence_response.status_code == 200, sequence_response.text
    api_listing, api_sequence = list_response.json(), sequence_response.json()
    assert api_listing["total"] == len(expected_ids)
    assert api_listing["offset"] == offset
    assert api_listing["limit"] == limit
    assert [item["id"] for item in api_listing["items"]] == expected_page
    assert api_sequence == {"items": sequence, "total": len(expected_sequence)}
    # Options remain available even when selections currently match no records.
    unfiltered = await store.list_generations({})
    assert api_listing["filters"] == listing["filters"] == unfiltered["filters"]


def test_unset_and_legacy_gallery_filters_keep_existing_behavior(gallery):
    async def run():
        for filters, expected in (
            ({}, "hgfedcba"),
            ({"provider_id": "natural"}, "gfa"),
            ({"mode": "img2img"}, "fc"),
            ({"source": "import"}, "hed"),
            ({"generation_engine": "nai"}, "hcb"),
            ({"generation_engine": "novelai"}, "hcb"),
            ({"provider_id": "", "mode": "", "source": ""}, "hgfedcba"),
        ):
            await assert_selection(gallery, filters, expected)

    asyncio.run(run())


@pytest.mark.parametrize(
    ("field", "values", "expected"),
    [
        ("provider_ids", ["natural", "nai"], "gfcba"),
        ("modes", ["text2img", "img2img"], "hgfecba"),
        ("sources", ["command", "llm_tool"], "gfcb"),
        ("generation_engines", ["openai_images", "novelai"], "hgfcba"),
    ],
)
def test_each_gallery_filter_accepts_multiple_choices(gallery, field, values, expected):
    asyncio.run(assert_selection(gallery, {field: values}, expected))


def test_all_explicit_choices_equal_unfiltered_gallery(gallery):
    filters = {
        "provider_ids": ["natural", "nai", "", LITERAL_PROVIDER_ID],
        "modes": ["text2img", "img2img", "unknown"],
        "sources": ["webui", "command", "llm_tool", "import"],
        "generation_engines": ["openai_images", "novelai", "comfyui", "unknown"],
    }
    asyncio.run(assert_selection(gallery, filters, "hgfedcba"))


@pytest.mark.parametrize("field", PLURAL_FIELDS)
def test_empty_selection_matches_nothing_and_overrides_legacy(gallery, field):
    singular, value, _ = PLURAL_FIELDS[field]
    asyncio.run(assert_selection(gallery, {singular: value, field: []}, ""))


@pytest.mark.parametrize(
    ("filters", "expected"),
    [
        ({"provider_id": "natural", "provider_ids": ["nai", "nai"]}, "cb"),
        ({"mode": "text2img", "modes": ["img2img", "img2img"]}, "fc"),
        ({"source": "webui", "sources": ["import", "import"]}, "hed"),
        (
            {"generation_engine": "openai_images", "generation_engines": ["nai"]},
            "hcb",
        ),
    ],
)
def test_plural_selection_overrides_legacy_without_duplicate_results(
    gallery, filters, expected
):
    asyncio.run(assert_selection(gallery, filters, expected))


def test_combined_multiselect_search_favorites_and_pagination(gallery):
    async def run():
        filters = {
            "provider_ids": ["natural", "nai"],
            "modes": ["text2img", "img2img"],
            "sources": ["webui", "command", "llm_tool"],
            "generation_engines": ["openai_images", "nai"],
            "favorite": "true",
            "query": "orchid",
            "limit": 1,
        }
        for offset in (0, 1, 2, 3):
            await assert_selection(gallery, {**filters, "offset": offset}, "gba")

    asyncio.run(run())


@pytest.mark.parametrize(
    ("values", "expected"),
    [
        ([""], "hd"),
        ([LITERAL_PROVIDER_ID], "e"),
        (["", LITERAL_PROVIDER_ID], "hed"),
        (["' OR 1=1 --"], ""),
    ],
)
def test_provider_ids_preserve_empty_and_literal_values(gallery, values, expected):
    async def run():
        await assert_selection(gallery, {"provider_ids": values}, expected)
        await assert_selection(gallery, {}, "hgfedcba")

    asyncio.run(run())


@pytest.mark.parametrize("values", [["nai"], ["novelai"], [" NAI ", "novelai", "nai"]])
def test_nai_engine_aliases_match_both_historical_spellings(gallery, values):
    asyncio.run(assert_selection(gallery, {"generation_engines": values}, "hcb"))


@pytest.mark.parametrize("field", PLURAL_FIELDS)
def test_unknown_choices_do_not_remove_the_filter(gallery, field):
    asyncio.run(assert_selection(gallery, {field: ["missing-value"]}, ""))


@pytest.mark.parametrize("field", PLURAL_FIELDS)
def test_invalid_http_multiselect_returns_400_on_both_routes(tmp_path, field):
    async def run():
        app = await create_app(tmp_path, seed=False)
        _, value, item_limit = PLURAL_FIELDS[field]
        invalid_values = [
            "",
            "[",
            "natural,nai",
            "null",
            "true",
            "1",
            '"natural"',
            "{}",
            "[null]",
            "[true]",
            "[1]",
            "[{}]",
            '[["natural"]]',
            "[" * 9000 + '"natural"' + "]" * 9000,
            json.dumps([value] * 257),
            json.dumps(["x" * (item_limit + 1)]),
            " " * 32767 + "[]",
        ]
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            for endpoint in ("gallery/list", "gallery/image-sequence"):
                for invalid in invalid_values:
                    response = await client.get(
                        PREFIX + endpoint, params={field: invalid}
                    )
                    assert response.status_code == 400, (endpoint, invalid[:100])
                    assert field in response.json()["message"]

    asyncio.run(run())


@pytest.mark.parametrize("field", PLURAL_FIELDS)
def test_storage_rejects_invalid_native_multiselect_values(tmp_path, field):
    async def run():
        app = await create_app(tmp_path, seed=False)
        store = app.state.plugin.store
        _, value, item_limit = PLURAL_FIELDS[field]
        invalid_values = [
            None,
            {},
            (value,),
            [None],
            [True],
            [1],
            [{}],
            [[value]],
            [value] * 257,
            ["x" * (item_limit + 1)],
        ]
        for invalid in invalid_values:
            with pytest.raises(ValueError, match=field):
                await store.list_generations({field: invalid})
            with pytest.raises(ValueError, match=field):
                await store.gallery_image_sequence({field: invalid})

    asyncio.run(run())


def test_multiselect_accepts_limits_without_truncating_values(gallery):
    async def run():
        filters = {}
        for field, (_, value, limit) in PLURAL_FIELDS.items():
            # Exercise all four limits together with 256 distinct values per
            # field; exactly one candidate in each field matches record a.
            filters[field] = [value] + [
                f"{index:03d}".ljust(limit, "x") for index in range(255)
            ]
        await assert_selection(gallery, filters, "a")

        app, _ = gallery
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            for endpoint in ("gallery/list", "gallery/image-sequence"):
                response = await client.get(
                    PREFIX + endpoint,
                    params={"provider_ids": " " * 32766 + "[]"},
                )
                assert response.status_code == 200, response.text
                assert response.json()["items"] == []
                assert response.json()["total"] == 0

    asyncio.run(run())
