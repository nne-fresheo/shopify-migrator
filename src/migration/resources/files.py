from __future__ import annotations

import asyncio
import logging
import mimetypes
import os
import tempfile
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse, urlunparse

import httpx

from .base import BaseResource, atomic_write_json
from ..id_map import IDMap

logger = logging.getLogger(__name__)

_GQL_GET_FILES = """
query getFiles($cursor: String) {
  files(first: 50, after: $cursor) {
    edges {
      node {
        id
        fileStatus
        ... on MediaImage {
          image { url }
          originalSource { url fileSize }
        }
        ... on GenericFile {
          url
          originalFileSize
          mimeType
        }
        ... on Video {
          originalSource { url fileSize mimeType }
        }
      }
    }
    pageInfo { hasNextPage endCursor }
  }
}
"""

_GQL_STAGED_UPLOADS = """
mutation stagedUploadsCreate($input: [StagedUploadInput!]!) {
  stagedUploadsCreate(input: $input) {
    stagedTargets {
      url
      resourceUrl
      parameters { name value }
    }
    userErrors { field message }
  }
}
"""

_GQL_FILE_CREATE = """
mutation fileCreate($files: [FileCreateInput!]!) {
  fileCreate(files: $files) {
    files {
      id
      fileStatus
      ... on MediaImage { image { url } }
      ... on GenericFile { url }
    }
    userErrors { field message }
  }
}
"""

_GQL_FILE_STATUS = """
query fileStatus($ids: [ID!]!) {
  nodes(ids: $ids) {
    ... on MediaImage { id fileStatus image { url } }
    ... on GenericFile { id fileStatus url }
    ... on Video { id fileStatus }
  }
}
"""

_BATCH_SIZE = 10
_POLL_INTERVAL = 3.0
_POLL_MAX = 15


class FilesResource(BaseResource):
    """
    Migrates Files section assets via the 3-step staged upload flow:
      1. stagedUploadsCreate  → S3 pre-signed URL
      2. Upload file to S3    → no Shopify auth needed
      3. fileCreate           → finalize in Shopify
    """

    resource_name = "files"
    endpoint = ""
    resource_key = ""
    list_key = ""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._s3_client = httpx.AsyncClient(timeout=httpx.Timeout(120.0))

    async def _fetch_all(self) -> list[dict]:
        all_files: list[dict] = []
        cursor = None

        while True:
            data = await self.source.graphql(
                _GQL_GET_FILES,
                variables={"cursor": cursor},
                estimated_cost=100,
            )
            edges = data.get("files", {}).get("edges", [])
            page_info = data.get("files", {}).get("pageInfo", {})

            for edge in edges:
                node = edge["node"]
                # Normalize: extract the primary URL regardless of file type
                node["_url"] = self._extract_url(node)
                node["_mime"] = self._extract_mime(node)
                node["_size"] = self._extract_size(node)
                if node["_url"]:
                    all_files.append(node)

            if not page_info.get("hasNextPage"):
                break
            cursor = page_info.get("endCursor")

        return all_files

    def _strip_query(self, url: str) -> str:
        """Remove query parameters from a URL (strips GCS signed URL expiry params)."""
        parsed = urlparse(url)
        return urlunparse(parsed._replace(query="", fragment=""))

    def _extract_url(self, node: dict) -> str | None:
        if node.get("image"):
            url = node["image"].get("url") or node.get("originalSource", {}).get("url")
            return self._strip_query(url) if url else None
        if "url" in node:
            return node["url"]
        src = node.get("originalSource", {})
        url = src.get("url")
        return self._strip_query(url) if url else None

    def _extract_mime(self, node: dict) -> str:
        src = node.get("originalSource", {})
        mime = src.get("mimeType") or node.get("mimeType")
        if not mime:
            url = self._extract_url(node) or ""
            mime, _ = mimetypes.guess_type(url)
        return mime or "application/octet-stream"

    def _extract_size(self, node: dict) -> int | None:
        src = node.get("originalSource", {})
        return src.get("fileSize") or node.get("originalFileSize")

    def transform(self, item: dict) -> dict:
        return item  # Handled inline

    async def load(self, force: bool = False) -> None:
        if not self._data_file.exists():
            logger.warning("[load] files: data file not found, skipping")
            return

        import json
        files: list[dict] = json.loads(self._data_file.read_text(encoding="utf-8"))
        logger.info(f"[load] files: starting ({len(files)} files)")

        # Process in batches
        for i in range(0, len(files), _BATCH_SIZE):
            batch = files[i: i + _BATCH_SIZE]
            await self._process_batch(batch, force)

        self.progress.mark_resource_done(self.resource_name)
        logger.info("[load] files: done")

    async def _process_batch(self, batch: list[dict], force: bool) -> None:
        # Filter out already-mapped files
        to_process = []
        for f in batch:
            src_id = f.get("id", "")
            if not force and self.id_map.has(src_id):
                logger.debug(f"[load] files: {src_id} already mapped, skipping")
                continue
            to_process.append(f)

        if not to_process:
            return

        # Step 1: Download files to temp dir and request staged upload targets
        temp_files = []
        staged_inputs = []
        file_meta = []

        tmp_dir = self.data_dir / "tmp"

        for f in to_process:
            url = f.get("_url")
            if not url:
                continue
            try:
                filename = self._url_to_filename(url)
                tmp_path = tmp_dir / filename
                size = await self._download_file(url, tmp_path)
                mime = f.get("_mime", "application/octet-stream")

                staged_inputs.append({
                    "filename": filename,
                    "mimeType": mime,
                    "fileSize": str(size),
                    "resource": self._resource_type(mime),
                    "httpMethod": "POST",
                })
                temp_files.append(tmp_path)
                file_meta.append(f)
            except Exception as exc:
                logger.error(f"[load] files: download failed for {url}: {exc}")

        if not staged_inputs:
            return

        # Step 2: Get staged upload targets
        try:
            data = await self.dest.graphql(
                _GQL_STAGED_UPLOADS,
                variables={"input": staged_inputs},
                estimated_cost=100,
            )
            result = data.get("stagedUploadsCreate", {})
            if result.get("userErrors"):
                logger.error(f"[load] files: stagedUploadsCreate errors: {result['userErrors']}")
                return
            targets = result.get("stagedTargets", [])
        except Exception as exc:
            logger.error(f"[load] files: stagedUploadsCreate failed: {exc}")
            return

        # Step 3: Upload to S3 and collect resourceUrls
        resource_urls = []
        for target, tmp_path, meta in zip(targets, temp_files, file_meta):
            try:
                await self._upload_to_s3(target, tmp_path)
                resource_urls.append((target["resourceUrl"], meta))
            except Exception as exc:
                logger.error(f"[load] files: S3 upload failed for {tmp_path.name}: {exc}")
            finally:
                try:
                    tmp_path.unlink(missing_ok=True)
                except Exception:
                    pass

        if not resource_urls:
            return

        # Step 4: fileCreate mutation
        files_input = [
            {"originalSource": resource_url, "contentType": self._resource_type(meta.get("_mime", ""))}
            for resource_url, meta in resource_urls
        ]
        try:
            data = await self.dest.graphql(
                _GQL_FILE_CREATE,
                variables={"files": files_input},
                estimated_cost=150,
            )
            result = data.get("fileCreate", {})
            if result.get("userErrors"):
                logger.error(f"[load] files: fileCreate errors: {result['userErrors']}")
                return
            created_files = result.get("files", [])
        except Exception as exc:
            logger.error(f"[load] files: fileCreate failed: {exc}")
            return

        # Step 5: Poll for READY status and record ID maps
        dest_gids = [f["id"] for f in created_files if f.get("id")]
        await self._poll_and_record(dest_gids, resource_urls)

    async def _poll_and_record(
        self, dest_gids: list[str], resource_urls: list[tuple[str, dict]]
    ) -> None:
        """Poll until all files are READY, then record source→dest URL mappings."""
        for attempt in range(_POLL_MAX):
            await asyncio.sleep(_POLL_INTERVAL)
            try:
                data = await self.dest.graphql(
                    _GQL_FILE_STATUS,
                    variables={"ids": dest_gids},
                    estimated_cost=50,
                )
                nodes = data.get("nodes", [])
                all_ready = all(n.get("fileStatus") == "READY" for n in nodes if n)
                if all_ready:
                    for node, (_, meta) in zip(nodes, resource_urls):
                        if not node:
                            continue
                        src_id = meta.get("id", "")
                        dest_url = (
                            node.get("image", {}).get("url")
                            or node.get("url")
                            or ""
                        )
                        src_url = meta.get("_url", "")
                        # Map source ID → dest ID
                        dest_id = node.get("id", "").split("/")[-1]
                        if src_id and dest_id:
                            self.id_map.set(src_id, dest_id)
                        # Also map source URL → dest URL for HTML rewriting
                        if src_url and dest_url:
                            self.id_map.set(src_url, dest_url)
                        logger.info(f"[load] files: {meta.get('_url', 'unknown')} → {dest_url}")
                    return
            except Exception as exc:
                logger.warning(f"[load] files: poll attempt {attempt + 1} failed: {exc}")

        logger.warning(f"[load] files: some files did not reach READY status after {_POLL_MAX} polls")

    async def _download_file(self, url: str, dest_path: Path) -> int:
        """Download a file to dest_path. Returns file size in bytes."""
        async with httpx.AsyncClient(follow_redirects=True, timeout=120.0) as client:
            async with client.stream("GET", url) as response:
                response.raise_for_status()
                size = 0
                with open(dest_path, "wb") as f:
                    async for chunk in response.aiter_bytes(chunk_size=65536):
                        f.write(chunk)
                        size += len(chunk)
        return size

    async def _upload_to_s3(self, target: dict, file_path: Path) -> None:
        """Upload file to S3 using the pre-signed multipart form from stagedUploadsCreate."""
        s3_url = target["url"]
        params = {p["name"]: p["value"] for p in target.get("parameters", [])}

        with open(file_path, "rb") as f:
            response = await self._s3_client.post(
                s3_url,
                data=params,
                files={"file": (file_path.name, f)},
            )
            response.raise_for_status()

    def _url_to_filename(self, url: str) -> str:
        parsed = urlparse(url)
        return Path(parsed.path).name or "file"

    def _resource_type(self, mime: str) -> str:
        if mime.startswith("image/"):
            return "IMAGE"
        if mime.startswith("video/"):
            return "VIDEO"
        return "FILE"

    async def find_existing(self, item: dict) -> Optional[dict]:
        return None

    async def __aexit__(self, *args) -> None:
        await self._s3_client.aclose()
        await super().__aexit__(*args)
