from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Optional


class ProgressTracker:
    """Tracks per-resource migration progress, persisted to progress.json."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._data: dict[str, dict] = {}
        self._load()

    def _load(self) -> None:
        if self._path.exists():
            try:
                self._data = json.loads(self._path.read_text(encoding="utf-8"))
            except Exception:
                self._data = {}

    def _save(self) -> None:
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._data, indent=2), encoding="utf-8")
        os.replace(tmp, self._path)

    def _resource(self, name: str) -> dict:
        if name not in self._data:
            self._data[name] = {
                "status": "pending",
                "extracted": 0,
                "loaded": 0,
                "failed": 0,
                "items": {},
            }
        return self._data[name]

    def mark_extracted(self, resource: str, count: int) -> None:
        self._resource(resource)["extracted"] = count
        self._save()

    def mark_item_done(self, resource: str, handle: str, dest_id: str) -> None:
        r = self._resource(resource)
        r["items"][handle] = {"status": "done", "dest_id": dest_id}
        r["loaded"] = r.get("loaded", 0) + 1
        self._save()

    def mark_item_failed(self, resource: str, handle: str, error: str) -> None:
        r = self._resource(resource)
        r["items"][handle] = {"status": "failed", "error": error}
        r["failed"] = r.get("failed", 0) + 1
        self._save()

    def is_item_done(self, resource: str, handle: str) -> bool:
        return (
            self._data.get(resource, {})
            .get("items", {})
            .get(handle, {})
            .get("status") == "done"
        )

    def mark_resource_done(self, resource: str) -> None:
        self._resource(resource)["status"] = "done"
        self._save()

    def is_resource_done(self, resource: str) -> bool:
        return self._data.get(resource, {}).get("status") == "done"

    def get_summary(self) -> dict[str, dict]:
        """Return per-resource summary without the per-item detail."""
        return {
            k: {kk: vv for kk, vv in v.items() if kk != "items"}
            for k, v in self._data.items()
        }
