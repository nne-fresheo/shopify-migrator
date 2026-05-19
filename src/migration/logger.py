from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

from rich.logging import RichHandler


def setup_logging(log_file: Path, console_level: str = "INFO") -> None:
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)

    # Console handler via Rich
    console_handler = RichHandler(
        level=getattr(logging, console_level.upper(), logging.INFO),
        rich_tracebacks=True,
        show_time=True,
        show_path=False,
    )
    root.addHandler(console_handler)

    # File handler — always at DEBUG
    file_handler = logging.FileHandler(log_file, mode="a", encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)-8s %(name)s: %(message)s")
    )
    root.addHandler(file_handler)


class FailedResourcesLog:
    """Appends failed resource entries to a JSON file."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._entries: list[dict] = []
        if path.exists():
            try:
                self._entries = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                self._entries = []

    def append(
        self,
        resource_type: str,
        source_id: str,
        error: str,
        payload: dict | None = None,
        handle: str | None = None,
    ) -> None:
        entry: dict = {
            "resource_type": resource_type,
            "source_id": source_id,
            "handle": handle,
            "error": error,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        if payload is not None:
            entry["payload"] = payload
        self._entries.append(entry)
        self._save()

    def _save(self) -> None:
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._entries, indent=2, default=str), encoding="utf-8")
        os.replace(tmp, self._path)

    def entries(self) -> list[dict]:
        return list(self._entries)
