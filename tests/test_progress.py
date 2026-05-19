from __future__ import annotations

import json
from pathlib import Path

import pytest

from migration.progress import ProgressTracker


class TestProgressTracker:
    def test_mark_and_check_item_done(self, tmp_log_dir: Path):
        p = ProgressTracker(tmp_log_dir / "progress.json")
        assert not p.is_item_done("products", "my-product")
        p.mark_item_done("products", "my-product", "999")
        assert p.is_item_done("products", "my-product")

    def test_mark_item_failed(self, tmp_log_dir: Path):
        p = ProgressTracker(tmp_log_dir / "progress.json")
        p.mark_item_failed("products", "bad-product", "422 error")
        assert not p.is_item_done("products", "bad-product")

    def test_mark_resource_done(self, tmp_log_dir: Path):
        p = ProgressTracker(tmp_log_dir / "progress.json")
        assert not p.is_resource_done("products")
        p.mark_resource_done("products")
        assert p.is_resource_done("products")

    def test_increments_loaded_counter(self, tmp_log_dir: Path):
        p = ProgressTracker(tmp_log_dir / "progress.json")
        p.mark_item_done("products", "p1", "1")
        p.mark_item_done("products", "p2", "2")
        summary = p.get_summary()
        assert summary["products"]["loaded"] == 2

    def test_increments_failed_counter(self, tmp_log_dir: Path):
        p = ProgressTracker(tmp_log_dir / "progress.json")
        p.mark_item_failed("products", "p1", "error")
        p.mark_item_failed("products", "p2", "error2")
        summary = p.get_summary()
        assert summary["products"]["failed"] == 2

    def test_persists_across_instances(self, tmp_log_dir: Path):
        path = tmp_log_dir / "progress.json"
        p1 = ProgressTracker(path)
        p1.mark_item_done("pages", "about-us", "456")

        p2 = ProgressTracker(path)
        assert p2.is_item_done("pages", "about-us")

    def test_atomic_write_no_tmp_file(self, tmp_log_dir: Path):
        path = tmp_log_dir / "progress.json"
        p = ProgressTracker(path)
        p.mark_item_done("pages", "test", "1")
        assert not (path.with_suffix(".tmp")).exists()

    def test_get_summary_excludes_items_detail(self, tmp_log_dir: Path):
        p = ProgressTracker(tmp_log_dir / "progress.json")
        p.mark_item_done("pages", "about", "1")
        summary = p.get_summary()
        assert "items" not in summary.get("pages", {})
