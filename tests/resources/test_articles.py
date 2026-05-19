from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from migration.id_map import IDMap
from migration.resources.articles import ArticlesResource


def _make_resource(source, dest, tmp_data_dir, progress, failed_log, blogs_id_map=None, dry_run=False):
    bim = IDMap(tmp_data_dir / "id_maps" / "blogs.json") if blogs_id_map is None else blogs_id_map
    return ArticlesResource(
        source_client=source,
        dest_client=dest,
        data_dir=tmp_data_dir,
        id_map=IDMap(tmp_data_dir / "id_maps" / "articles.json"),
        progress=progress,
        failed_log=failed_log,
        blogs_id_map=bim,
        dry_run=dry_run,
    )


class TestArticlesTransform:
    def test_strips_reserved_fields(self):
        resource = _make_resource(AsyncMock(), AsyncMock(), Path("/tmp"), MagicMock(), MagicMock())
        article = {
            "id": 5,
            "admin_graphql_api_id": "gid://shopify/Article/5",
            "blog_id": 10,
            "user_id": 1,
            "title": "Hello",
            "handle": "hello",
            "body_html": "<p>Hi</p>",
            "published_at": "2024-01-01",
            "created_at": "2024-01-01",
            "updated_at": "2024-01-02",
            "_source_blog_id": "10",
            "_source_blog_id_for_create": "10",
        }
        result = resource.transform(article)
        # _source_blog_id_for_create is intentionally kept so _create can pop it
        for field in ("id", "admin_graphql_api_id", "blog_id", "user_id", "published_at", "_source_blog_id"):
            assert field not in result
        assert result["title"] == "Hello"
        assert result["body_html"] == "<p>Hi</p>"


class TestArticlesExtract:
    async def test_fetches_articles_per_blog(
        self, mock_source_client, mock_dest_client, tmp_data_dir, progress, failed_log
    ):
        mock_source_client.get = AsyncMock(
            return_value={"blogs": [{"id": 10}, {"id": 20}]}
        )

        async def mock_paginated(path, key, **kwargs):
            if "10" in path:
                yield [{"id": 1, "handle": "art-1", "title": "Art 1", "body_html": ""}]
            else:
                yield [{"id": 2, "handle": "art-2", "title": "Art 2", "body_html": ""}]

        mock_source_client.get_paginated = mock_paginated

        resource = _make_resource(mock_source_client, mock_dest_client, tmp_data_dir, progress, failed_log)
        result = await resource.extract()

        assert len(result) == 2
        blog_ids = {a["_source_blog_id"] for a in result}
        assert blog_ids == {"10", "20"}

    async def test_handles_empty_blogs(
        self, mock_source_client, mock_dest_client, tmp_data_dir, progress, failed_log
    ):
        mock_source_client.get = AsyncMock(return_value={"blogs": []})

        resource = _make_resource(mock_source_client, mock_dest_client, tmp_data_dir, progress, failed_log)
        result = await resource.extract()

        assert result == []


class TestArticlesLoad:
    async def test_creates_article_under_correct_blog(
        self, mock_source_client, mock_dest_client, tmp_data_dir, progress, failed_log
    ):
        # Set up blogs ID map
        blogs_id_map = IDMap(tmp_data_dir / "id_maps" / "blogs.json")
        blogs_id_map.set("10", "99")

        data_file = tmp_data_dir / "articles.json"
        data_file.write_text(json.dumps([{
            "id": 5,
            "handle": "hello",
            "title": "Hello",
            "body_html": "<p>Hi</p>",
            "_source_blog_id": "10",
        }]))

        mock_dest_client.get = AsyncMock(return_value={"articles": []})
        mock_dest_client.post = AsyncMock(return_value={"article": {"id": 55}})

        resource = _make_resource(
            mock_source_client, mock_dest_client, tmp_data_dir, progress, failed_log,
            blogs_id_map=blogs_id_map
        )
        await resource.load()

        call_args = mock_dest_client.post.call_args
        assert "blogs/99/articles.json" in call_args[0][0]
        assert resource.id_map.get("5") == "55"

    async def test_skips_when_blog_not_mapped(
        self, mock_source_client, mock_dest_client, tmp_data_dir, progress, failed_log
    ):
        data_file = tmp_data_dir / "articles.json"
        data_file.write_text(json.dumps([{
            "id": 5,
            "handle": "hello",
            "title": "Hello",
            "_source_blog_id": "99",  # no mapping for 99
        }]))

        resource = _make_resource(mock_source_client, mock_dest_client, tmp_data_dir, progress, failed_log)
        await resource.load()

        mock_dest_client.post.assert_not_called()

    async def test_skips_existing_by_handle(
        self, mock_source_client, mock_dest_client, tmp_data_dir, progress, failed_log
    ):
        blogs_id_map = IDMap(tmp_data_dir / "id_maps" / "blogs.json")
        blogs_id_map.set("10", "99")

        data_file = tmp_data_dir / "articles.json"
        data_file.write_text(json.dumps([{
            "id": 5,
            "handle": "hello",
            "title": "Hello",
            "_source_blog_id": "10",
        }]))

        mock_dest_client.get = AsyncMock(
            return_value={"articles": [{"id": 77, "handle": "hello"}]}
        )

        resource = _make_resource(
            mock_source_client, mock_dest_client, tmp_data_dir, progress, failed_log,
            blogs_id_map=blogs_id_map
        )
        await resource.load()

        mock_dest_client.post.assert_not_called()
        assert resource.id_map.get("5") == "77"
