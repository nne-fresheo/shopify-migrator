from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from migration.id_map import IDMap
from migration.resources.discount_codes import DiscountCodesResource


def _make_resource(source, dest, tmp_data_dir, progress, failed_log, price_rules_id_map=None, dry_run=False):
    prim = IDMap(tmp_data_dir / "id_maps" / "price_rules.json") if price_rules_id_map is None else price_rules_id_map
    return DiscountCodesResource(
        source_client=source,
        dest_client=dest,
        data_dir=tmp_data_dir,
        id_map=IDMap(tmp_data_dir / "id_maps" / "discount_codes.json"),
        progress=progress,
        failed_log=failed_log,
        price_rules_id_map=prim,
        dry_run=dry_run,
    )


class TestDiscountCodesTransform:
    def test_strips_source_fields(self):
        resource = _make_resource(AsyncMock(), AsyncMock(), Path("/tmp"), MagicMock(), MagicMock())
        code = {
            "id": 1,
            "code": "SAVE10",
            "price_rule_id": 5,
            "usage_count": 3,
            "errors": {},
            "created_at": "2024-01-01",
            "updated_at": "2024-01-02",
            "_source_price_rule_id": "5",
            "_dest_price_rule_id": "50",
        }
        result = resource.transform(code)
        # _dest_price_rule_id is intentionally kept so _create can pop it for routing
        for field in ("id", "price_rule_id", "usage_count", "errors", "_source_price_rule_id"):
            assert field not in result
        assert result["code"] == "SAVE10"


class TestDiscountCodesExtract:
    async def test_fetches_codes_per_price_rule(
        self, mock_source_client, mock_dest_client, tmp_data_dir, progress, failed_log
    ):
        async def mock_paginated(path, key, **kwargs):
            if "price_rules.json" in path:
                yield [{"id": 10}, {"id": 20}]
            elif "10" in path:
                yield [{"id": 1, "code": "A", "usage_count": 0}]
            elif "20" in path:
                yield [{"id": 2, "code": "B", "usage_count": 0}]
            else:
                yield []

        mock_source_client.get_paginated = mock_paginated

        resource = _make_resource(mock_source_client, mock_dest_client, tmp_data_dir, progress, failed_log)
        result = await resource.extract()

        assert len(result) == 2
        rule_ids = {c["_source_price_rule_id"] for c in result}
        assert rule_ids == {"10", "20"}

    async def test_handles_empty_price_rules(
        self, mock_source_client, mock_dest_client, tmp_data_dir, progress, failed_log
    ):
        async def mock_paginated(path, key, **kwargs):
            yield []

        mock_source_client.get_paginated = mock_paginated

        resource = _make_resource(mock_source_client, mock_dest_client, tmp_data_dir, progress, failed_log)
        result = await resource.extract()

        assert result == []


class TestDiscountCodesLoad:
    async def test_creates_code_under_correct_price_rule(
        self, mock_source_client, mock_dest_client, tmp_data_dir, progress, failed_log
    ):
        price_rules_id_map = IDMap(tmp_data_dir / "id_maps" / "price_rules.json")
        price_rules_id_map.set("10", "100")

        data_file = tmp_data_dir / "discount_codes.json"
        data_file.write_text(json.dumps([{
            "id": 1,
            "code": "SAVE10",
            "usage_count": 0,
            "_source_price_rule_id": "10",
        }]))

        mock_dest_client.get = AsyncMock(return_value={"discount_codes": []})
        mock_dest_client.post = AsyncMock(return_value={"discount_code": {"id": 55}})

        resource = _make_resource(
            mock_source_client, mock_dest_client, tmp_data_dir, progress, failed_log,
            price_rules_id_map=price_rules_id_map
        )
        await resource.load()

        call_args = mock_dest_client.post.call_args
        assert "price_rules/100/discount_codes.json" in call_args[0][0]
        assert resource.id_map.get("1") == "55"

    async def test_skips_when_price_rule_not_mapped(
        self, mock_source_client, mock_dest_client, tmp_data_dir, progress, failed_log
    ):
        data_file = tmp_data_dir / "discount_codes.json"
        data_file.write_text(json.dumps([{
            "id": 1,
            "code": "SAVE10",
            "_source_price_rule_id": "99",  # not mapped
        }]))

        resource = _make_resource(mock_source_client, mock_dest_client, tmp_data_dir, progress, failed_log)
        await resource.load()

        mock_dest_client.post.assert_not_called()

    async def test_skips_existing_by_code(
        self, mock_source_client, mock_dest_client, tmp_data_dir, progress, failed_log
    ):
        price_rules_id_map = IDMap(tmp_data_dir / "id_maps" / "price_rules.json")
        price_rules_id_map.set("10", "100")

        data_file = tmp_data_dir / "discount_codes.json"
        data_file.write_text(json.dumps([{
            "id": 1,
            "code": "SAVE10",
            "_source_price_rule_id": "10",
        }]))

        mock_dest_client.get = AsyncMock(
            return_value={"discount_codes": [{"id": 77, "code": "SAVE10"}]}
        )

        resource = _make_resource(
            mock_source_client, mock_dest_client, tmp_data_dir, progress, failed_log,
            price_rules_id_map=price_rules_id_map
        )
        await resource.load()

        mock_dest_client.post.assert_not_called()
        assert resource.id_map.get("1") == "77"
