from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from migration.id_map import IDMap
from migration.resources.gift_cards import GiftCardsResource


def _make_resource(source, dest, tmp_data_dir, progress, failed_log, dry_run=False):
    return GiftCardsResource(
        source_client=source,
        dest_client=dest,
        data_dir=tmp_data_dir,
        id_map=IDMap(tmp_data_dir / "id_maps" / "gift_cards.json"),
        progress=progress,
        failed_log=failed_log,
        dry_run=dry_run,
    )


class TestGiftCardsTransform:
    def test_strips_read_only_fields(self):
        resource = _make_resource(AsyncMock(), AsyncMock(), Path("/tmp"), MagicMock(), MagicMock())
        card = {
            "id": 1,
            "code": "ABCD-1234-EFGH-5678",
            "initial_value": "50.00",
            "balance": "30.00",
            "currency": "USD",
            "last_characters": "5678",
            "disabled_at": None,
            "line_item_id": None,
            "order_id": None,
            "customer": None,
            "template_suffix": None,
            "created_at": "2024-01-01",
            "updated_at": "2024-01-02",
        }
        result = resource.transform(card)
        for field in ("id", "balance", "currency", "last_characters", "disabled_at",
                      "line_item_id", "order_id", "customer", "template_suffix"):
            assert field not in result
        assert result["code"] == "ABCD-1234-EFGH-5678"
        assert result["initial_value"] == "50.00"

    async def test_no_idempotency_check(self, tmp_data_dir, progress, failed_log):
        resource = _make_resource(AsyncMock(), AsyncMock(), tmp_data_dir, progress, failed_log)
        # find_existing should return None (can't look up by partial code)
        result = await resource.find_existing({"id": 1, "code": "ABCD-1234"})
        assert result is None


class TestGiftCardsLoad:
    async def test_creates_gift_card(
        self, mock_source_client, mock_dest_client, tmp_data_dir, progress, failed_log
    ):
        data_file = tmp_data_dir / "gift_cards.json"
        data_file.write_text(json.dumps([{
            "id": 1,
            "code": "ABCD-1234-EFGH-5678",
            "initial_value": "50.00",
            "balance": "50.00",
        }]))

        mock_dest_client.get = AsyncMock(return_value={})
        mock_dest_client.post = AsyncMock(return_value={"gift_card": {"id": 99}})

        resource = _make_resource(mock_source_client, mock_dest_client, tmp_data_dir, progress, failed_log)
        await resource.load()

        mock_dest_client.post.assert_called_once()
        assert resource.id_map.get("1") == "99"

    async def test_handles_api_error_isolates(
        self, mock_source_client, mock_dest_client, tmp_data_dir, progress, failed_log
    ):
        data_file = tmp_data_dir / "gift_cards.json"
        data_file.write_text(json.dumps([
            {"id": 1, "code": "AAA-BBB", "initial_value": "10.00"},
            {"id": 2, "code": "CCC-DDD", "initial_value": "20.00"},
        ]))

        mock_dest_client.get = AsyncMock(return_value={})
        mock_dest_client.post = AsyncMock(side_effect=[
            Exception("422"),
            {"gift_card": {"id": 88}},
        ])

        resource = _make_resource(mock_source_client, mock_dest_client, tmp_data_dir, progress, failed_log)
        await resource.load()

        assert resource.id_map.get("2") == "88"
        assert len(failed_log.entries()) == 1

    async def test_dry_run(
        self, mock_source_client, mock_dest_client, tmp_data_dir, progress, failed_log
    ):
        data_file = tmp_data_dir / "gift_cards.json"
        data_file.write_text(json.dumps([{
            "id": 1,
            "code": "ABCD-1234-EFGH-5678",
            "initial_value": "50.00",
        }]))

        mock_dest_client.get = AsyncMock(return_value={})

        resource = _make_resource(mock_source_client, mock_dest_client, tmp_data_dir, progress, failed_log, dry_run=True)
        await resource.load()

        mock_dest_client.post.assert_not_called()
