from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from migration.id_map import IDMap
from migration.resources.blogs import BlogsResource


def _make_resource(source, dest, tmp_data_dir, progress, failed_log, dry_run=False):
    return BlogsResource(
        source_client=source,
        dest_client=dest,
        data_dir=tmp_data_dir,
        id_map=IDMap(tmp_data_dir / "id_maps" / "blogs.json"),
        progress=progress,
        failed_log=failed_log,
        dry_run=dry_run,
    )


class TestBlogsTransform:
    def test_strips_base_and_extra_fields(self):
        resource = _make_resource(AsyncMock(), AsyncMock(), Path("/tmp"), MagicMock(), MagicMock())
        blog = {
            "id": 1,
            "admin_graphql_api_id": "gid://shopify/OnlineStoreBlog/1",
            "title": "News",
            "handle": "news",
            "commentable": "no",
            "published_at": "2024-01-01",
            "created_at": "2024-01-01",
            "updated_at": "2024-01-02",
        }
        result = resource.transform(blog)
        assert "id" not in result
        assert "admin_graphql_api_id" not in result
        assert "published_at" not in result
        assert result["title"] == "News"
        assert result["handle"] == "news"

    def test_keeps_commentable(self):
        resource = _make_resource(AsyncMock(), AsyncMock(), Path("/tmp"), MagicMock(), MagicMock())
        blog = {"id": 1, "title": "Blog", "handle": "blog", "commentable": "moderate"}
        result = resource.transform(blog)
        assert result["commentable"] == "moderate"


class TestBlogsLoad:
    async def test_skips_existing_by_handle(
        self, mock_source_client, mock_dest_client, tmp_data_dir, progress, failed_log
    ):
        data_file = tmp_data_dir / "blogs.json"
        data_file.write_text(json.dumps([{"id": 10, "handle": "news", "title": "News"}]))

        mock_dest_client.get = AsyncMock(
            return_value={"blogs": [{"id": 99, "handle": "news"}]}
        )

        resource = _make_resource(mock_source_client, mock_dest_client, tmp_data_dir, progress, failed_log)
        await resource.load()

        mock_dest_client.post.assert_not_called()
        assert resource.id_map.get("10") == "99"

    async def test_creates_new_blog(
        self, mock_source_client, mock_dest_client, tmp_data_dir, progress, failed_log
    ):
        data_file = tmp_data_dir / "blogs.json"
        data_file.write_text(json.dumps([{"id": 10, "handle": "news", "title": "News"}]))

        mock_dest_client.get = AsyncMock(return_value={"blogs": []})
        mock_dest_client.post = AsyncMock(return_value={"blog": {"id": 88, "handle": "news"}})

        resource = _make_resource(mock_source_client, mock_dest_client, tmp_data_dir, progress, failed_log)
        await resource.load()

        mock_dest_client.post.assert_called_once()
        assert resource.id_map.get("10") == "88"
        assert progress.is_item_done("blogs", "news")

    async def test_logs_failure_and_continues(
        self, mock_source_client, mock_dest_client, tmp_data_dir, progress, failed_log
    ):
        data_file = tmp_data_dir / "blogs.json"
        data_file.write_text(json.dumps([
            {"id": 1, "handle": "blog-a", "title": "A"},
            {"id": 2, "handle": "blog-b", "title": "B"},
        ]))

        mock_dest_client.get = AsyncMock(return_value={"blogs": []})
        mock_dest_client.post = AsyncMock(side_effect=[
            Exception("422 error"),
            {"blog": {"id": 77, "handle": "blog-b"}},
        ])

        resource = _make_resource(mock_source_client, mock_dest_client, tmp_data_dir, progress, failed_log)
        await resource.load()

        assert resource.id_map.get("2") == "77"
        assert len(failed_log.entries()) == 1

    async def test_dry_run_skips_creation(
        self, mock_source_client, mock_dest_client, tmp_data_dir, progress, failed_log
    ):
        data_file = tmp_data_dir / "blogs.json"
        data_file.write_text(json.dumps([{"id": 10, "handle": "news", "title": "News"}]))

        mock_dest_client.get = AsyncMock(return_value={"blogs": []})

        resource = _make_resource(mock_source_client, mock_dest_client, tmp_data_dir, progress, failed_log, dry_run=True)
        await resource.load()

        mock_dest_client.post.assert_not_called()


class TestBlogsExtract:
    async def test_paginates_all_blogs(
        self, mock_source_client, mock_dest_client, tmp_data_dir, progress, failed_log
    ):
        page1 = [{"id": 1, "handle": "blog-1"}]
        page2 = [{"id": 2, "handle": "blog-2"}]

        call_count = 0

        async def mock_paginated(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            yield page1
            yield page2

        mock_source_client.get_paginated = mock_paginated

        resource = _make_resource(mock_source_client, mock_dest_client, tmp_data_dir, progress, failed_log)
        result = await resource.extract()

        assert len(result) == 2
        data_file = tmp_data_dir / "blogs.json"
        assert data_file.exists()

    async def test_handles_empty_store(
        self, mock_source_client, mock_dest_client, tmp_data_dir, progress, failed_log
    ):
        async def mock_paginated(*args, **kwargs):
            yield []

        mock_source_client.get_paginated = mock_paginated

        resource = _make_resource(mock_source_client, mock_dest_client, tmp_data_dir, progress, failed_log)
        result = await resource.extract()

        assert result == []
        assert (tmp_data_dir / "blogs.json").exists()
