from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from migration.id_map import IDMap
from migration.resources.pages import PagesResource


def _make_resource(source, dest, tmp_data_dir, progress, failed_log, dry_run=False):
    return PagesResource(
        source_client=source,
        dest_client=dest,
        data_dir=tmp_data_dir,
        id_map=IDMap(tmp_data_dir / "id_maps" / "pages.json"),
        progress=progress,
        failed_log=failed_log,
        dry_run=dry_run,
    )


class TestPagesTransform:
    def test_strips_base_fields(self):
        resource = _make_resource(
            AsyncMock(), AsyncMock(), Path("/tmp"), MagicMock(), MagicMock()
        )
        page = {
            "id": 10,
            "admin_graphql_api_id": "gid://shopify/OnlineStorePage/10",
            "shop_id": 5,
            "title": "About Us",
            "handle": "about-us",
            "body_html": "<p>About</p>",
            "published_at": "2024-01-01",
            "created_at": "2024-01-01",
            "updated_at": "2024-01-02",
        }
        result = resource.transform(page)
        assert "id" not in result
        assert "admin_graphql_api_id" not in result
        assert "shop_id" not in result
        assert "published_at" not in result
        assert result["title"] == "About Us"
        assert result["body_html"] == "<p>About</p>"


class TestPagesExtract:
    async def test_saves_to_file(self, mock_source_client, mock_dest_client, tmp_data_dir, progress, failed_log):
        pages = [{"id": 1, "handle": "about-us", "title": "About Us", "body_html": ""}]

        async def mock_paginated(*args, **kwargs):
            yield pages
        mock_source_client.get_paginated = mock_paginated

        resource = _make_resource(mock_source_client, mock_dest_client, tmp_data_dir, progress, failed_log)
        await resource.extract()

        data_file = tmp_data_dir / "pages.json"
        assert data_file.exists()
        saved = json.loads(data_file.read_text())
        assert len(saved) == 1
        assert saved[0]["handle"] == "about-us"

    async def test_skips_if_file_exists(self, mock_source_client, mock_dest_client, tmp_data_dir, progress, failed_log):
        # Pre-existing file
        data_file = tmp_data_dir / "pages.json"
        data_file.write_text(json.dumps([{"id": 1, "handle": "cached"}]))

        resource = _make_resource(mock_source_client, mock_dest_client, tmp_data_dir, progress, failed_log)
        result = await resource.extract()

        # Should not call source API
        mock_source_client.get_paginated.assert_not_called()
        assert result[0]["handle"] == "cached"

    async def test_force_re_extracts(self, mock_source_client, mock_dest_client, tmp_data_dir, progress, failed_log):
        data_file = tmp_data_dir / "pages.json"
        data_file.write_text(json.dumps([{"id": 1, "handle": "cached"}]))

        fresh_pages = [{"id": 2, "handle": "fresh"}]

        async def mock_paginated(*args, **kwargs):
            yield fresh_pages
        mock_source_client.get_paginated = mock_paginated

        resource = _make_resource(mock_source_client, mock_dest_client, tmp_data_dir, progress, failed_log)
        result = await resource.extract(force=True)

        assert result[0]["handle"] == "fresh"
