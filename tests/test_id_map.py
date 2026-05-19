from __future__ import annotations

import json
from pathlib import Path

import pytest

from migration.id_map import IDMap, IDMapRegistry, _strip_gid


class TestStripGid:
    def test_strips_gid_prefix(self):
        assert _strip_gid("gid://shopify/Product/123") == "123"

    def test_passes_through_plain_id(self):
        assert _strip_gid("123456") == "123456"

    def test_handles_int(self):
        assert _strip_gid(123) == "123"


class TestIDMap:
    def test_set_and_get(self, tmp_data_dir: Path):
        m = IDMap(tmp_data_dir / "id_maps" / "products.json")
        m.set("111", "999")
        assert m.get("111") == "999"

    def test_has(self, tmp_data_dir: Path):
        m = IDMap(tmp_data_dir / "id_maps" / "products.json")
        assert not m.has("111")
        m.set("111", "999")
        assert m.has("111")

    def test_set_strips_gid(self, tmp_data_dir: Path):
        m = IDMap(tmp_data_dir / "id_maps" / "products.json")
        m.set("gid://shopify/Product/111", "gid://shopify/Product/999")
        assert m.get("111") == "999"

    def test_atomic_write(self, tmp_data_dir: Path):
        path = tmp_data_dir / "id_maps" / "products.json"
        m = IDMap(path)
        m.set("1", "2")
        # File should exist and contain correct data
        assert path.exists()
        data = json.loads(path.read_text())
        assert data == {"1": "2"}

    def test_no_tmp_file_left_over(self, tmp_data_dir: Path):
        path = tmp_data_dir / "id_maps" / "products.json"
        m = IDMap(path)
        m.set("1", "2")
        tmp_path = path.with_suffix(".tmp")
        assert not tmp_path.exists()

    def test_round_trip_persistence(self, tmp_data_dir: Path):
        path = tmp_data_dir / "id_maps" / "products.json"
        m1 = IDMap(path)
        m1.set("100", "200")
        m1.set("101", "201")

        m2 = IDMap(path)
        assert m2.get("100") == "200"
        assert m2.get("101") == "201"

    def test_len(self, tmp_data_dir: Path):
        m = IDMap(tmp_data_dir / "id_maps" / "products.json")
        assert len(m) == 0
        m.set("1", "2")
        assert len(m) == 1


class TestIDMapRegistry:
    def test_returns_same_instance(self, tmp_data_dir: Path):
        registry = IDMapRegistry(tmp_data_dir / "id_maps")
        m1 = registry.get("products")
        m2 = registry.get("products")
        assert m1 is m2

    def test_separate_maps_per_resource(self, tmp_data_dir: Path):
        registry = IDMapRegistry(tmp_data_dir / "id_maps")
        registry.get("products").set("1", "100")
        registry.get("collections").set("1", "200")
        assert registry.get("products").get("1") == "100"
        assert registry.get("collections").get("1") == "200"
