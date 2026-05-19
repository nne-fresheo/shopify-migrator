from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from migration.id_map import IDMap
from migration.resources.price_rules import PriceRulesResource


def _make_resource(source, dest, tmp_data_dir, progress, failed_log, collections_id_map=None, variants_id_map=None, dry_run=False):
    cim = IDMap(tmp_data_dir / "id_maps" / "collections.json") if collections_id_map is None else collections_id_map
    return PriceRulesResource(
        source_client=source,
        dest_client=dest,
        data_dir=tmp_data_dir,
        id_map=IDMap(tmp_data_dir / "id_maps" / "price_rules.json"),
        progress=progress,
        failed_log=failed_log,
        collections_id_map=cim,
        variants_id_map=variants_id_map,
        dry_run=dry_run,
    )


class TestPriceRulesTransform:
    def test_strips_base_fields(self, tmp_data_dir):
        resource = _make_resource(AsyncMock(), AsyncMock(), tmp_data_dir, MagicMock(), MagicMock())
        rule = {
            "id": 1,
            "title": "10OFF",
            "value": "-10.0",
            "value_type": "fixed_amount",
            "usage_count": 5,
            "created_at": "2024-01-01",
            "updated_at": "2024-01-02",
            "entitled_collection_ids": [],
            "prerequisite_collection_ids": [],
        }
        result = resource.transform(rule)
        assert "id" not in result
        assert "usage_count" not in result
        assert result["title"] == "10OFF"

    def test_remaps_entitled_collection_ids(self, tmp_data_dir):
        collections_id_map = IDMap(tmp_data_dir / "id_maps" / "collections.json")
        collections_id_map.set("10", "100")
        collections_id_map.set("20", "200")
        resource = _make_resource(AsyncMock(), AsyncMock(), tmp_data_dir, MagicMock(), MagicMock(),
                                   collections_id_map=collections_id_map)

        rule = {
            "id": 1,
            "title": "SALE",
            "entitled_collection_ids": ["10", "20", "99"],  # 99 not mapped
            "prerequisite_collection_ids": [],
        }
        result = resource.transform(rule)
        assert result["entitled_collection_ids"] == ["100", "200"]

    def test_remaps_prerequisite_collection_ids(self, tmp_data_dir):
        collections_id_map = IDMap(tmp_data_dir / "id_maps" / "collections.json")
        collections_id_map.set("5", "50")
        resource = _make_resource(AsyncMock(), AsyncMock(), tmp_data_dir, MagicMock(), MagicMock(),
                                   collections_id_map=collections_id_map)

        rule = {
            "id": 1,
            "title": "PREREQ",
            "entitled_collection_ids": [],
            "prerequisite_collection_ids": ["5"],
        }
        result = resource.transform(rule)
        assert result["prerequisite_collection_ids"] == ["50"]


class TestPriceRulesLoad:
    async def test_creates_price_rule(
        self, mock_source_client, mock_dest_client, tmp_data_dir, progress, failed_log
    ):
        data_file = tmp_data_dir / "price_rules.json"
        data_file.write_text(json.dumps([{
            "id": 1,
            "title": "10OFF",
            "value": "-10.0",
            "value_type": "fixed_amount",
            "entitled_collection_ids": [],
            "prerequisite_collection_ids": [],
        }]))

        mock_dest_client.get = AsyncMock(return_value={"price_rules": []})
        mock_dest_client.post = AsyncMock(return_value={"price_rule": {"id": 99}})

        resource = _make_resource(mock_source_client, mock_dest_client, tmp_data_dir, progress, failed_log)
        await resource.load()

        mock_dest_client.post.assert_called_once()
        assert resource.id_map.get("1") == "99"

    async def test_skips_existing_by_title(
        self, mock_source_client, mock_dest_client, tmp_data_dir, progress, failed_log
    ):
        data_file = tmp_data_dir / "price_rules.json"
        data_file.write_text(json.dumps([{
            "id": 1,
            "title": "10OFF",
            "entitled_collection_ids": [],
            "prerequisite_collection_ids": [],
        }]))

        mock_dest_client.get = AsyncMock(return_value={"price_rules": [{"id": 77, "title": "10OFF"}]})

        resource = _make_resource(mock_source_client, mock_dest_client, tmp_data_dir, progress, failed_log)
        await resource.load()

        mock_dest_client.post.assert_not_called()
        assert resource.id_map.get("1") == "77"

    async def test_handles_api_error_isolates(
        self, mock_source_client, mock_dest_client, tmp_data_dir, progress, failed_log
    ):
        data_file = tmp_data_dir / "price_rules.json"
        data_file.write_text(json.dumps([
            {"id": 1, "title": "RULE1", "entitled_collection_ids": [], "prerequisite_collection_ids": []},
            {"id": 2, "title": "RULE2", "entitled_collection_ids": [], "prerequisite_collection_ids": []},
        ]))

        mock_dest_client.get = AsyncMock(return_value={"price_rules": []})
        mock_dest_client.post = AsyncMock(side_effect=[
            Exception("422"),
            {"price_rule": {"id": 88}},
        ])

        resource = _make_resource(mock_source_client, mock_dest_client, tmp_data_dir, progress, failed_log)
        await resource.load()

        assert resource.id_map.get("2") == "88"
        assert len(failed_log.entries()) == 1


class TestPriceRulesVariantMapping:
    def test_remaps_entitled_variant_ids_when_map_provided(self, tmp_data_dir):
        variants_id_map = IDMap(tmp_data_dir / "id_maps" / "variants.json")
        variants_id_map.set("1001", "9001")
        variants_id_map.set("1002", "9002")
        resource = _make_resource(
            AsyncMock(), AsyncMock(), tmp_data_dir, MagicMock(), MagicMock(),
            variants_id_map=variants_id_map,
        )
        rule = {
            "id": 5,
            "title": "BUNDLE",
            "target_selection": "entitled",
            "allocation_method": "each",
            "entitled_variant_ids": [1001, 1002, 1003],  # 1003 not mapped
            "entitled_product_ids": [],
            "entitled_collection_ids": [],
            "prerequisite_collection_ids": [],
            "prerequisite_variant_ids": [],
            "prerequisite_product_ids": [],
        }
        result = resource.transform(rule)
        assert result["entitled_variant_ids"] == ["9001", "9002"]
        # Still "entitled" because there are mapped items
        assert result["target_selection"] == "entitled"

    def test_fallback_each_with_no_entitled_items_changes_to_across_all(self, tmp_data_dir):
        # No variants_id_map → all cleared → fallback must produce "across"+"all" (not "each"+"all")
        resource = _make_resource(
            AsyncMock(), AsyncMock(), tmp_data_dir, MagicMock(), MagicMock(),
        )
        rule = {
            "id": 6,
            "title": "UNMAPPABLE",
            "target_selection": "entitled",
            "allocation_method": "each",
            "entitled_variant_ids": [9999],
            "entitled_product_ids": [],
            "entitled_collection_ids": [],
            "prerequisite_collection_ids": [],
            "prerequisite_variant_ids": [],
            "prerequisite_product_ids": [],
        }
        result = resource.transform(rule)
        assert result["target_selection"] == "all"
        assert result["allocation_method"] == "across"

    def test_fallback_across_with_no_entitled_items_keeps_across(self, tmp_data_dir):
        resource = _make_resource(
            AsyncMock(), AsyncMock(), tmp_data_dir, MagicMock(), MagicMock(),
        )
        rule = {
            "id": 7,
            "title": "ACROSS_RULE",
            "target_selection": "entitled",
            "allocation_method": "across",
            "entitled_variant_ids": [8888],
            "entitled_product_ids": [],
            "entitled_collection_ids": [],
            "prerequisite_collection_ids": [],
            "prerequisite_variant_ids": [],
            "prerequisite_product_ids": [],
        }
        result = resource.transform(rule)
        assert result["target_selection"] == "all"
        assert result["allocation_method"] == "across"
