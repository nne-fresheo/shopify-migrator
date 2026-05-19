from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from migration.client import ShopifyClient
from migration.id_map import IDMap, IDMapRegistry
from migration.logger import FailedResourcesLog
from migration.progress import ProgressTracker


@pytest.fixture
def tmp_data_dir(tmp_path: Path) -> Path:
    d = tmp_path / "data"
    d.mkdir()
    (d / "id_maps").mkdir()
    (d / "tmp").mkdir()
    return d


@pytest.fixture
def tmp_log_dir(tmp_path: Path) -> Path:
    d = tmp_path / "logs"
    d.mkdir()
    return d


@pytest.fixture
def id_map(tmp_data_dir: Path) -> IDMap:
    return IDMap(tmp_data_dir / "id_maps" / "test.json")


@pytest.fixture
def id_map_registry(tmp_data_dir: Path) -> IDMapRegistry:
    return IDMapRegistry(tmp_data_dir / "id_maps")


@pytest.fixture
def progress(tmp_log_dir: Path) -> ProgressTracker:
    return ProgressTracker(tmp_log_dir / "progress.json")


@pytest.fixture
def failed_log(tmp_log_dir: Path) -> FailedResourcesLog:
    return FailedResourcesLog(tmp_log_dir / "failed_resources.json")


@pytest.fixture
def mock_source_client() -> AsyncMock:
    client = AsyncMock(spec=ShopifyClient)
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=None)
    return client


@pytest.fixture
def mock_dest_client() -> AsyncMock:
    client = AsyncMock(spec=ShopifyClient)
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=None)
    return client


@pytest.fixture
def sample_product() -> dict:
    return {
        "id": 111,
        "admin_graphql_api_id": "gid://shopify/Product/111",
        "title": "Test Product",
        "handle": "test-product",
        "body_html": "<p>Description</p>",
        "vendor": "Test Vendor",
        "product_type": "Widget",
        "status": "active",
        "tags": "tag1,tag2",
        "created_at": "2024-01-01T00:00:00Z",
        "updated_at": "2024-01-02T00:00:00Z",
        "variants": [
            {
                "id": 222,
                "product_id": 111,
                "inventory_item_id": 333,
                "admin_graphql_api_id": "gid://shopify/ProductVariant/222",
                "title": "Default Title",
                "price": "29.99",
                "sku": "SKU-001",
                "barcode": "12345",
                "option1": "Default Title",
                "option2": None,
                "option3": None,
                "inventory_management": "shopify",
                "created_at": "2024-01-01T00:00:00Z",
                "updated_at": "2024-01-02T00:00:00Z",
            }
        ],
        "options": [{"id": 1, "product_id": 111, "name": "Title", "position": 1}],
        "images": [
            {
                "id": 444,
                "product_id": 111,
                "admin_graphql_api_id": "gid://shopify/ProductImage/444",
                "src": "https://cdn.shopify.com/s/files/1/0001/test.jpg",
                "position": 1,
                "alt": "Test image",
                "variant_ids": [],
                "created_at": "2024-01-01T00:00:00Z",
                "updated_at": "2024-01-02T00:00:00Z",
            }
        ],
    }
