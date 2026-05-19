from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Iterator, Optional


def _strip_gid(value: str | int) -> str:
    """Convert 'gid://shopify/Product/123' -> '123'. Pass through plain IDs as strings."""
    s = str(value)
    return s.split("/")[-1] if s.startswith("gid://") else s


class IDMap:
    """Persists source_id → dest_id mappings for a single resource type."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._data: dict[str, str] = {}
        self._load()

    def _load(self) -> None:
        if self._path.exists():
            try:
                raw = json.loads(self._path.read_text(encoding="utf-8"))
                self._data = {str(k): str(v) for k, v in raw.items()}
            except Exception:
                self._data = {}

    def _save(self) -> None:
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._data, indent=2), encoding="utf-8")
        os.replace(tmp, self._path)

    def set(self, source_id: str | int, dest_id: str | int) -> None:
        self._data[_strip_gid(source_id)] = _strip_gid(dest_id)
        self._save()

    def get(self, source_id: str | int) -> Optional[str]:
        return self._data.get(_strip_gid(str(source_id)))

    def has(self, source_id: str | int) -> bool:
        return _strip_gid(str(source_id)) in self._data

    def items(self) -> Iterator[tuple[str, str]]:
        return iter(self._data.items())

    def __len__(self) -> int:
        return len(self._data)


class IDMapRegistry:
    """Central registry of ID maps, one per resource type."""

    def __init__(self, id_maps_dir: Path) -> None:
        self._dir = id_maps_dir
        self._maps: dict[str, IDMap] = {}

    def get(self, resource_name: str) -> IDMap:
        if resource_name not in self._maps:
            self._maps[resource_name] = IDMap(self._dir / f"{resource_name}.json")
        return self._maps[resource_name]
