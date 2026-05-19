from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from migration.resources.variant_id_map_builder import build_variant_id_map


def _make_paginated(pages: list[list[dict]]):
    """Return an async generator function that yields the given pages."""
    async def _gen(*args, **kwargs):
        for page in pages:
            yield page
    return _gen


@pytest.mark.asyncio
async def test_build_matches_variants_by_sku(tmp_path):
    source = AsyncMock()
    dest = AsyncMock()

    source.get_paginated = _make_paginated([[
        {"id": 1, "variants": [{"id": 101, "sku": "SKU-A"}, {"id": 102, "sku": "SKU-B"}]}
    ]])
    dest.get_paginated = _make_paginated([[
        {"id": 2, "variants": [{"id": 201, "sku": "SKU-A"}, {"id": 202, "sku": "SKU-B"}]}
    ]])

    result = await build_variant_id_map(source, dest)

    assert result == {"101": "201", "102": "202"}


@pytest.mark.asyncio
async def test_build_skips_blank_skus(tmp_path):
    source = AsyncMock()
    dest = AsyncMock()

    source.get_paginated = _make_paginated([[
        {"id": 1, "variants": [{"id": 101, "sku": ""}, {"id": 102, "sku": None}]}
    ]])
    dest.get_paginated = _make_paginated([[
        {"id": 2, "variants": [{"id": 201, "sku": ""}, {"id": 202, "sku": None}]}
    ]])

    result = await build_variant_id_map(source, dest)

    assert result == {}


@pytest.mark.asyncio
async def test_build_skips_unmatched_source_skus(tmp_path):
    source = AsyncMock()
    dest = AsyncMock()

    source.get_paginated = _make_paginated([[
        {"id": 1, "variants": [{"id": 101, "sku": "SKU-ONLY-IN-SOURCE"}]}
    ]])
    dest.get_paginated = _make_paginated([[
        {"id": 2, "variants": [{"id": 201, "sku": "DIFFERENT-SKU"}]}
    ]])

    result = await build_variant_id_map(source, dest)

    assert result == {}


@pytest.mark.asyncio
async def test_build_excludes_duplicate_skus_in_source(tmp_path):
    source = AsyncMock()
    dest = AsyncMock()

    source.get_paginated = _make_paginated([[
        {"id": 1, "variants": [{"id": 101, "sku": "DUP"}, {"id": 102, "sku": "DUP"}]},
        # Two products with same SKU variant
    ]])
    dest.get_paginated = _make_paginated([[
        {"id": 2, "variants": [{"id": 201, "sku": "DUP"}]}
    ]])

    result = await build_variant_id_map(source, dest)

    # Duplicate in source → both excluded from result
    assert "101" not in result
    assert "102" not in result


@pytest.mark.asyncio
async def test_build_loads_from_cache(tmp_path):
    cache = tmp_path / "variants.json"
    cache.write_text(json.dumps({"101": "201", "102": "202"}), encoding="utf-8")

    source = AsyncMock()
    dest = AsyncMock()

    result = await build_variant_id_map(source, dest, cache_path=cache)

    assert result == {"101": "201", "102": "202"}
    source.get_paginated.assert_not_called()
    dest.get_paginated.assert_not_called()


@pytest.mark.asyncio
async def test_build_saves_to_cache(tmp_path):
    cache = tmp_path / "variants.json"

    source = AsyncMock()
    dest = AsyncMock()

    source.get_paginated = _make_paginated([[
        {"id": 1, "variants": [{"id": 101, "sku": "SKU-A"}]}
    ]])
    dest.get_paginated = _make_paginated([[
        {"id": 2, "variants": [{"id": 201, "sku": "SKU-A"}]}
    ]])

    result = await build_variant_id_map(source, dest, cache_path=cache)

    assert cache.exists()
    saved = json.loads(cache.read_text(encoding="utf-8"))
    assert saved == {"101": "201"}


@pytest.mark.asyncio
async def test_build_falls_back_on_corrupt_cache(tmp_path):
    cache = tmp_path / "variants.json"
    cache.write_text("NOT JSON", encoding="utf-8")

    source = AsyncMock()
    dest = AsyncMock()

    source.get_paginated = _make_paginated([[
        {"id": 1, "variants": [{"id": 101, "sku": "SKU-A"}]}
    ]])
    dest.get_paginated = _make_paginated([[
        {"id": 2, "variants": [{"id": 201, "sku": "SKU-A"}]}
    ]])

    result = await build_variant_id_map(source, dest, cache_path=cache)

    # Falls back to live fetch, gets correct result
    assert result == {"101": "201"}
