from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .utils import read_json, write_json


class PaperLibraryStore:
    """Profile-scoped, local collection of papers explicitly saved by the user."""

    def __init__(self, output_root: Path, profile_id: str) -> None:
        suffix = profile_id or "default"
        self.path = output_root / f"paper-library-{suffix}.json"

    def all(self) -> dict[str, dict[str, Any]]:
        payload = read_json(self.path, {}) or {}
        items = payload.get("items", payload)
        return items if isinstance(items, dict) else {}

    def contains(self, arxiv_id: str) -> bool:
        return arxiv_id in self.all()

    def add(
        self,
        item: dict[str, Any],
        profile_name: str,
        source_date: str = "",
    ) -> dict[str, Any]:
        paper = item.get("paper") or {}
        arxiv_id = str(paper.get("arxiv_id") or "").strip()
        if not arxiv_id:
            raise ValueError("论文缺少 arXiv ID，无法加入文献库")
        items = self.all()
        previous = items.get(arxiv_id) or {}
        entry = {
            "arxiv_id": arxiv_id,
            "profile_name": profile_name,
            "source_date": source_date,
            "saved_at": previous.get("saved_at")
            or datetime.now(timezone.utc).isoformat(),
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "paper": paper,
            "verified": item.get("verified") or {},
        }
        items[arxiv_id] = entry
        self._write(items)
        return entry

    def remove(self, arxiv_id: str) -> bool:
        items = self.all()
        removed = items.pop(arxiv_id, None) is not None
        if removed:
            self._write(items)
        return removed

    def _write(self, items: dict[str, dict[str, Any]]) -> None:
        write_json(self.path, {"version": 1, "items": items})
