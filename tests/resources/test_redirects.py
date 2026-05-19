from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from migration.id_map import IDMap
from migration.resources.redirects import RedirectsResource


def _make_resource(source, dest, tmp_data_dir, progress, failed_log, dry_run=False):
    return RedirectsResource(
        source_client=source,
        dest_client=dest,
        data_dir=tmp_data_dir,
        id_map=IDMap(tmp_data_dir / "id_maps" / "redirects.json"),
        progress=progress,
        failed_log=failed_log,
        dry_run=dry_run,
    )


class TestRedirectsTransform:
    def test_strips_id_and_timestamps(self):
        resource = _make_resource(AsyncMock(), AsyncMock(), Path("/tmp"), MagicMock(), MagicMock())
        redirect = {
            "id": 1,
            "path": "/old-path",
            "target": "/new-path",
            "created_at": "2024-01-01",
            "updated_at": "2024-01-02",
        }
        result = resource.transform(redirect)
        assert "id" not in result
        assert "created_at" not in result
        assert result["path"] == "/old-path"
        assert result["target"] == "/new-path"


class TestRedirectsLoad:
    async def test_skips_existing_by_path(
        self, mock_source_client, mock_dest_client, tmp_data_dir, progress, failed_log
    ):
        data_file = tmp_data_dir / "redirects.json"
        data_file.write_text(json.dumps([
            {"id": 1, "path": "/old", "target": "/new"},
        ]))

        mock_dest_client.get = AsyncMock(
            return_value={"redirects": [{"id": 55, "path": "/old"}]}
        )

        resource = _make_resource(mock_source_client, mock_dest_client, tmp_data_dir, progress, failed_log)
        await resource.load()

        mock_dest_client.post.assert_not_called()
        assert resource.id_map.get("1") == "55"

    async def test_creates_redirect(
        self, mock_source_client, mock_dest_client, tmp_data_dir, progress, failed_log
    ):
        data_file = tmp_data_dir / "redirects.json"
        data_file.write_text(json.dumps([
            {"id": 1, "path": "/old", "target": "/new"},
        ]))

        mock_dest_client.get = AsyncMock(return_value={"redirects": []})
        mock_dest_client.post = AsyncMock(return_value={"redirect": {"id": 66}})

        resource = _make_resource(mock_source_client, mock_dest_client, tmp_data_dir, progress, failed_log)
        await resource.load()

        mock_dest_client.post.assert_called_once()
        assert resource.id_map.get("1") == "66"

    async def test_handles_api_error(
        self, mock_source_client, mock_dest_client, tmp_data_dir, progress, failed_log
    ):
        data_file = tmp_data_dir / "redirects.json"
        data_file.write_text(json.dumps([
            {"id": 1, "path": "/old", "target": "/new"},
            {"id": 2, "path": "/old2", "target": "/new2"},
        ]))

        mock_dest_client.get = AsyncMock(return_value={"redirects": []})
        mock_dest_client.post = AsyncMock(side_effect=[
            Exception("422 error"),
            {"redirect": {"id": 77}},
        ])

        resource = _make_resource(mock_source_client, mock_dest_client, tmp_data_dir, progress, failed_log)
        await resource.load()

        assert resource.id_map.get("2") == "77"
        assert len(failed_log.entries()) == 1
