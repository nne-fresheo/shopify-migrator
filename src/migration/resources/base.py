from __future__ import annotations

import json
import logging
import os
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Optional

from ..client import ShopifyClient
from ..id_map import IDMap
from ..logger import FailedResourcesLog
from ..progress import ProgressTracker

logger = logging.getLogger(__name__)

# Fields that should never be sent to the destination API
_BASE_STRIP_FIELDS = frozenset({"id", "admin_graphql_api_id", "created_at", "updated_at"})


def atomic_write_json(path: Path, data: Any) -> None:
    """Write data to path atomically (write to .tmp then os.replace)."""
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")
    os.replace(tmp, path)


class BaseResource(ABC):
    """
    Abstract base class for all migration resource modules.

    Subclasses must define:
      - resource_name: str   e.g. "products"
      - endpoint: str        e.g. "products.json"
      - resource_key: str    e.g. "product"   (singular, for POST/PUT payload)
      - list_key: str        e.g. "products"  (plural, from GET response)

    And implement:
      - transform(item) -> dict
      - find_existing(item) -> Optional[dict]  (for idempotency)
    """

    resource_name: str
    endpoint: str
    resource_key: str
    list_key: str

    def __init__(
        self,
        source_client: Optional[ShopifyClient],
        dest_client: ShopifyClient,
        data_dir: Path,
        id_map: IDMap,
        progress: ProgressTracker,
        failed_log: FailedResourcesLog,
        dry_run: bool = False,
    ) -> None:
        self.source = source_client
        self.dest = dest_client
        self.data_dir = data_dir
        self.id_map = id_map
        self.progress = progress
        self.failed_log = failed_log
        self.dry_run = dry_run
        self._data_file = data_dir / f"{self.resource_name}.json"

    # ── EXTRACT ──────────────────────────────────────────────────────────────

    async def extract(self, force: bool = False) -> list[dict]:
        if not force and self._data_file.exists():
            logger.info(
                f"[extract] {self.resource_name}: already extracted, skipping "
                f"(use --force to re-extract)"
            )
            return json.loads(self._data_file.read_text(encoding="utf-8"))

        logger.info(f"[extract] {self.resource_name}: starting")
        items = await self._fetch_all()
        atomic_write_json(self._data_file, items)
        self.progress.mark_extracted(self.resource_name, len(items))
        logger.info(f"[extract] {self.resource_name}: done ({len(items)} items)")
        return items

    async def _fetch_all(self) -> list[dict]:
        """Default implementation: paginate the REST list endpoint."""
        all_items: list[dict] = []
        async for page in self.source.get_paginated(self.endpoint, self.list_key):
            all_items.extend(page)
            logger.debug(f"[extract] {self.resource_name}: {len(all_items)} fetched so far")
        return all_items

    # ── LOAD ─────────────────────────────────────────────────────────────────

    async def load(self, force: bool = False) -> None:
        if not self._data_file.exists():
            logger.warning(f"[load] {self.resource_name}: data file not found, skipping")
            return

        items: list[dict] = json.loads(self._data_file.read_text(encoding="utf-8"))
        logger.info(f"[load] {self.resource_name}: starting ({len(items)} items)")

        for item in items:
            await self._load_item(item, force)

        self.progress.mark_resource_done(self.resource_name)
        logger.info(f"[load] {self.resource_name}: done")

    async def _load_item(self, item: dict, force: bool = False) -> Optional[str]:
        """Process a single item: idempotency check → transform → create → record."""
        source_id = str(item.get("id", ""))
        handle = item.get("handle") or item.get("title") or item.get("code") or source_id

        # Already mapped in a previous run
        if not force and self.id_map.has(source_id):
            logger.debug(f"[load] {self.resource_name} '{handle}': already mapped, skipping")
            return self.id_map.get(source_id)

        # Already marked done in progress tracker
        if not force and self.progress.is_item_done(self.resource_name, handle):
            logger.warning(f"[load] {self.resource_name} '{handle}': already done, skipping")
            return None

        try:
            # Idempotency: check if resource exists on destination
            existing = await self.find_existing(item)
            if existing:
                dest_id = str(existing.get("id", ""))
                self.id_map.set(source_id, dest_id)
                if not force:
                    logger.warning(
                        f"[load] {self.resource_name} '{handle}': "
                        f"already exists on dest (id={dest_id}), skipping"
                    )
                return dest_id

            payload = self.transform(item)

            if self.dry_run:
                logger.info(f"[DRY RUN] would create {self.resource_name} '{handle}'")
                return None

            dest_id = await self._create(payload)
            self.id_map.set(source_id, dest_id)
            self.progress.mark_item_done(self.resource_name, handle, dest_id)
            logger.info(f"[load] {self.resource_name} '{handle}': created (dest_id={dest_id})")
            return dest_id

        except Exception as exc:
            logger.error(f"[load] {self.resource_name} '{handle}': FAILED — {exc}")
            self.failed_log.append(
                resource_type=self.resource_name,
                source_id=source_id,
                handle=handle,
                error=str(exc),
                payload=item,
            )
            self.progress.mark_item_failed(self.resource_name, handle, str(exc))
            return None  # Isolation: continue to next item

    async def _create(self, payload: dict) -> str:
        """Default: POST to endpoint, return destination ID as string."""
        response = await self.dest.post(self.endpoint, {self.resource_key: payload})
        resource = response.get(self.resource_key, {})
        return str(resource["id"])

    # ── ABSTRACT ─────────────────────────────────────────────────────────────

    @abstractmethod
    def transform(self, item: dict) -> dict:
        """Strip source IDs, remap foreign IDs, prepare payload for destination API."""
        ...

    async def find_existing(self, item: dict) -> Optional[dict]:
        """Check if resource already exists on destination. Return it or None."""
        return None
