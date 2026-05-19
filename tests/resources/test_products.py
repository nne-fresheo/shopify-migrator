from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from migration.id_map import IDMap
from migration.logger import FailedResourcesLog
from migration.progress import ProgressTracker
from migration.resources.products import ProductsResource


def _make_resource(
    source, dest, tmp_data_dir, progress, failed_log, dry_run=False
) -> ProductsResource:
    return ProductsResource(
        source_client=source,
        dest_client=dest,
        data_dir=tmp_data_dir,
        id_map=IDMap(tmp_data_dir / "id_maps" / "products.json"),
        progress=progress,
        failed_log=failed_log,
        dry_run=dry_run,
    )


class TestProductsTransform:
    def test_strips_base_fields(self, sample_product):
        resource = _make_resource(
            AsyncMock(), AsyncMock(), Path("/tmp"), MagicMock(), MagicMock()
        )
        result = resource.transform(sample_product)
        assert "id" not in result
        assert "admin_graphql_api_id" not in result
        assert "created_at" not in result
        assert "updated_at" not in result

    def test_keeps_product_fields(self, sample_product):
        resource = _make_resource(
            AsyncMock(), AsyncMock(), Path("/tmp"), MagicMock(), MagicMock()
        )
        result = resource.transform(sample_product)
        assert result["title"] == "Test Product"
        assert result["handle"] == "test-product"
        assert result["body_html"] == "<p>Description</p>"

    def test_strips_variant_ids(self, sample_product):
        resource = _make_resource(
            AsyncMock(), AsyncMock(), Path("/tmp"), MagicMock(), MagicMock()
        )
        result = resource.transform(sample_product)
        variant = result["variants"][0]
        assert "id" not in variant
        assert "product_id" not in variant
        assert "inventory_item_id" not in variant

    def test_keeps_variant_price_and_sku(self, sample_product):
        resource = _make_resource(
            AsyncMock(), AsyncMock(), Path("/tmp"), MagicMock(), MagicMock()
        )
        result = resource.transform(sample_product)
        variant = result["variants"][0]
        assert variant["price"] == "29.99"
        assert variant["sku"] == "SKU-001"

    def test_keeps_image_src(self, sample_product):
        resource = _make_resource(
            AsyncMock(), AsyncMock(), Path("/tmp"), MagicMock(), MagicMock()
        )
        result = resource.transform(sample_product)
        assert len(result["images"]) == 1
        assert result["images"][0]["src"].startswith("https://cdn.shopify.com")

    def test_strips_image_ids(self, sample_product):
        resource = _make_resource(
            AsyncMock(), AsyncMock(), Path("/tmp"), MagicMock(), MagicMock()
        )
        result = resource.transform(sample_product)
        img = result["images"][0]
        assert "id" not in img
        assert "product_id" not in img
        assert "variant_ids" not in img

    def test_excludes_images_without_src(self, sample_product):
        sample_product["images"][0]["src"] = None
        resource = _make_resource(
            AsyncMock(), AsyncMock(), Path("/tmp"), MagicMock(), MagicMock()
        )
        result = resource.transform(sample_product)
        assert result["images"] == []


class TestProductsLoad:
    async def test_skips_existing_by_handle(
        self, mock_source_client, mock_dest_client, tmp_data_dir, progress, failed_log, sample_product
    ):
        # Write data file
        data_file = tmp_data_dir / "products.json"
        data_file.write_text(json.dumps([sample_product]))

        # Dest finds existing product by handle
        mock_dest_client.get = AsyncMock(
            return_value={"products": [{"id": 999, "handle": "test-product"}]}
        )

        resource = _make_resource(
            mock_source_client, mock_dest_client, tmp_data_dir, progress, failed_log
        )
        await resource.load()

        # Should NOT have called post (no creation)
        mock_dest_client.post.assert_not_called()
        # Should have recorded the ID mapping
        assert resource.id_map.get("111") == "999"

    async def test_creates_new_product(
        self, mock_source_client, mock_dest_client, tmp_data_dir, progress, failed_log, sample_product
    ):
        data_file = tmp_data_dir / "products.json"
        data_file.write_text(json.dumps([sample_product]))

        # No existing product on dest
        mock_dest_client.get = AsyncMock(return_value={"products": []})
        mock_dest_client.post = AsyncMock(
            return_value={"product": {"id": 888, "handle": "test-product"}}
        )

        resource = _make_resource(
            mock_source_client, mock_dest_client, tmp_data_dir, progress, failed_log
        )
        await resource.load()

        mock_dest_client.post.assert_called_once()
        assert resource.id_map.get("111") == "888"
        assert progress.is_item_done("products", "test-product")

    async def test_isolates_failure(
        self, mock_source_client, mock_dest_client, tmp_data_dir, progress, failed_log
    ):
        products = [
            {"id": 1, "handle": "product-1", "title": "P1", "variants": [], "images": []},
            {"id": 2, "handle": "product-2", "title": "P2", "variants": [], "images": []},
        ]
        data_file = tmp_data_dir / "products.json"
        data_file.write_text(json.dumps(products))

        # No existing products
        mock_dest_client.get = AsyncMock(return_value={"products": []})
        # First product fails, second succeeds
        mock_dest_client.post = AsyncMock(
            side_effect=[
                Exception("422 Unprocessable"),
                {"product": {"id": 999, "handle": "product-2"}},
            ]
        )

        resource = _make_resource(
            mock_source_client, mock_dest_client, tmp_data_dir, progress, failed_log
        )
        await resource.load()

        # Second product should still be created
        assert resource.id_map.get("2") == "999"
        # Failed resource should be logged
        assert len(failed_log.entries()) == 1
        assert failed_log.entries()[0]["source_id"] == "1"

    async def test_dry_run_skips_creation(
        self, mock_source_client, mock_dest_client, tmp_data_dir, progress, failed_log, sample_product
    ):
        data_file = tmp_data_dir / "products.json"
        data_file.write_text(json.dumps([sample_product]))

        mock_dest_client.get = AsyncMock(return_value={"products": []})

        resource = _make_resource(
            mock_source_client, mock_dest_client, tmp_data_dir, progress, failed_log, dry_run=True
        )
        await resource.load()

        mock_dest_client.post.assert_not_called()

    async def test_skips_if_already_mapped(
        self, mock_source_client, mock_dest_client, tmp_data_dir, progress, failed_log, sample_product
    ):
        data_file = tmp_data_dir / "products.json"
        data_file.write_text(json.dumps([sample_product]))

        resource = _make_resource(
            mock_source_client, mock_dest_client, tmp_data_dir, progress, failed_log
        )
        # Pre-populate id map
        resource.id_map.set("111", "777")

        await resource.load()

        mock_dest_client.get.assert_not_called()
        mock_dest_client.post.assert_not_called()


# Required for pytest to find the MagicMock import
from unittest.mock import MagicMock
