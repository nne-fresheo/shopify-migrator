from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from migration.id_map import IDMap
from migration.resources.collections import CollectionsResource


def _make_resource(source, dest, tmp_data_dir, progress, failed_log, products_id_map=None, dry_run=False):
    pim = IDMap(tmp_data_dir / "id_maps" / "products.json") if products_id_map is None else products_id_map
    return CollectionsResource(
        source_client=source,
        dest_client=dest,
        data_dir=tmp_data_dir,
        id_map=IDMap(tmp_data_dir / "id_maps" / "collections.json"),
        progress=progress,
        failed_log=failed_log,
        products_id_map=pim,
        dry_run=dry_run,
    )


class TestCollectionsTransform:
    def test_strips_base_fields(self):
        resource = _make_resource(AsyncMock(), AsyncMock(), Path("/tmp"), MagicMock(), MagicMock())
        coll = {
            "id": 1,
            "admin_graphql_api_id": "gid://shopify/Collection/1",
            "handle": "sale",
            "title": "Sale",
            "published_at": "2024-01-01",
            "created_at": "2024-01-01",
            "updated_at": "2024-01-02",
            "_type": "custom",
        }
        result = resource.transform(coll)
        # _type is intentionally kept so _create can pop it to route to the correct endpoint
        for field in ("id", "admin_graphql_api_id", "published_at"):
            assert field not in result
        assert result["title"] == "Sale"


class TestCollectionsExtract:
    async def test_extracts_custom_and_smart(
        self, mock_source_client, mock_dest_client, tmp_data_dir, progress, failed_log
    ):
        custom = [{"id": 1, "handle": "custom-1", "title": "Custom"}]
        smart = [{"id": 2, "handle": "smart-1", "title": "Smart", "rules": []}]

        async def mock_paginated(path, key, **kwargs):
            if "custom" in path:
                yield custom
            else:
                yield smart

        mock_source_client.get_paginated = mock_paginated

        resource = _make_resource(mock_source_client, mock_dest_client, tmp_data_dir, progress, failed_log)
        result = await resource.extract()

        assert len(result) == 2
        types = {c["_type"] for c in result}
        assert types == {"custom", "smart"}

    async def test_also_extracts_collects(
        self, mock_source_client, mock_dest_client, tmp_data_dir, progress, failed_log
    ):
        async def mock_paginated(path, key, **kwargs):
            if "custom" in path or "smart" in path:
                yield []
            elif "collects" in path:
                yield [{"id": 1, "collection_id": 10, "product_id": 20}]
            else:
                yield []

        mock_source_client.get_paginated = mock_paginated

        resource = _make_resource(mock_source_client, mock_dest_client, tmp_data_dir, progress, failed_log)
        await resource.extract()

        collects_file = tmp_data_dir / "collection_memberships.json"
        assert collects_file.exists()
        data = json.loads(collects_file.read_text())
        assert len(data) == 1


class TestCollectionsLoad:
    async def test_creates_custom_collection(
        self, mock_source_client, mock_dest_client, tmp_data_dir, progress, failed_log
    ):
        data_file = tmp_data_dir / "collections.json"
        data_file.write_text(json.dumps([
            {"id": 1, "handle": "sale", "title": "Sale", "_type": "custom"},
        ]))

        mock_dest_client.get = AsyncMock(return_value={"custom_collections": []})
        mock_dest_client.post = AsyncMock(
            return_value={"custom_collection": {"id": 99, "handle": "sale"}}
        )

        resource = _make_resource(mock_source_client, mock_dest_client, tmp_data_dir, progress, failed_log)
        await resource.load()

        call_args = mock_dest_client.post.call_args
        assert "custom_collections.json" in call_args[0][0]
        assert resource.id_map.get("1") == "99"

    async def test_creates_smart_collection(
        self, mock_source_client, mock_dest_client, tmp_data_dir, progress, failed_log
    ):
        data_file = tmp_data_dir / "collections.json"
        data_file.write_text(json.dumps([
            {"id": 2, "handle": "new-arrivals", "title": "New Arrivals", "_type": "smart", "rules": []},
        ]))

        mock_dest_client.get = AsyncMock(return_value={"smart_collections": []})
        mock_dest_client.post = AsyncMock(
            return_value={"smart_collection": {"id": 88, "handle": "new-arrivals"}}
        )

        resource = _make_resource(mock_source_client, mock_dest_client, tmp_data_dir, progress, failed_log)
        await resource.load()

        call_args = mock_dest_client.post.call_args
        assert "smart_collections.json" in call_args[0][0]
        assert resource.id_map.get("2") == "88"

    async def test_skips_existing_custom(
        self, mock_source_client, mock_dest_client, tmp_data_dir, progress, failed_log
    ):
        data_file = tmp_data_dir / "collections.json"
        data_file.write_text(json.dumps([
            {"id": 1, "handle": "sale", "title": "Sale", "_type": "custom"},
        ]))

        mock_dest_client.get = AsyncMock(
            return_value={"custom_collections": [{"id": 77, "handle": "sale"}]}
        )

        resource = _make_resource(mock_source_client, mock_dest_client, tmp_data_dir, progress, failed_log)
        await resource.load()

        mock_dest_client.post.assert_not_called()
        assert resource.id_map.get("1") == "77"


class TestCollectionMemberships:
    async def test_creates_memberships(
        self, mock_source_client, mock_dest_client, tmp_data_dir, progress, failed_log
    ):
        products_id_map = IDMap(tmp_data_dir / "id_maps" / "products.json")
        products_id_map.set("20", "200")

        collects_file = tmp_data_dir / "collection_memberships.json"
        collects_file.write_text(json.dumps([
            {"id": 1, "collection_id": 10, "product_id": 20},
        ]))

        resource = _make_resource(
            mock_source_client, mock_dest_client, tmp_data_dir, progress, failed_log,
            products_id_map=products_id_map
        )
        resource.id_map.set("10", "100")

        mock_dest_client.post = AsyncMock(return_value={"collect": {"id": 999}})
        await resource.load_memberships()

        call_args = mock_dest_client.post.call_args
        assert call_args[0][1]["collect"]["product_id"] == "200"
        assert call_args[0][1]["collect"]["collection_id"] == "100"

    async def test_skips_unmapped_product(
        self, mock_source_client, mock_dest_client, tmp_data_dir, progress, failed_log
    ):
        collects_file = tmp_data_dir / "collection_memberships.json"
        collects_file.write_text(json.dumps([
            {"id": 1, "collection_id": 10, "product_id": 99},  # 99 not in products map
        ]))

        resource = _make_resource(mock_source_client, mock_dest_client, tmp_data_dir, progress, failed_log)
        resource.id_map.set("10", "100")

        await resource.load_memberships()

        mock_dest_client.post.assert_not_called()

    async def test_dry_run_skips_create(
        self, mock_source_client, mock_dest_client, tmp_data_dir, progress, failed_log
    ):
        products_id_map = IDMap(tmp_data_dir / "id_maps" / "products.json")
        products_id_map.set("20", "200")

        collects_file = tmp_data_dir / "collection_memberships.json"
        collects_file.write_text(json.dumps([
            {"id": 1, "collection_id": 10, "product_id": 20},
        ]))

        resource = _make_resource(
            mock_source_client, mock_dest_client, tmp_data_dir, progress, failed_log,
            products_id_map=products_id_map, dry_run=True
        )
        resource.id_map.set("10", "100")

        await resource.load_memberships()

        mock_dest_client.post.assert_not_called()
