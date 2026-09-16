from __future__ import annotations

from pathlib import Path
from typing import Any

from .reading_state import ReadingStateStore


class PaperLibraryStore:
    """Profile-scoped, local collection of papers explicitly saved by the user."""

    def __init__(self, output_root: Path, profile_id: str) -> None:
        self.state = ReadingStateStore(output_root, profile_id)
        self.path = self.state.path

    def all(self) -> dict[str, dict[str, Any]]:
        return self.state.snapshot()["library"]

    def contains(self, arxiv_id: str) -> bool:
        return arxiv_id in self.all()

    def add(
        self,
        item: dict[str, Any],
        profile_name: str,
        source_date: str = "",
    ) -> dict[str, Any]:
        return self.state.save(self._entry(item, profile_name, source_date))

    def toggle(self, item: dict[str, Any], profile_name: str, source_date: str = "") -> bool:
        return self.state.toggle(self._entry(item, profile_name, source_date))

    @staticmethod
    def _entry(item: dict[str, Any], profile_name: str, source_date: str) -> dict[str, Any]:
        paper = item.get("paper") or {}
        arxiv_id = str(paper.get("arxiv_id") or "").strip()
        if not arxiv_id:
            raise ValueError("论文缺少 arXiv ID，无法加入文献库")
        entry = {
            "arxiv_id": arxiv_id,
            "profile_name": profile_name,
            "source_date": source_date,
            "paper": paper,
            "verified": item.get("verified") or {},
        }
        return entry

    def remove(self, arxiv_id: str) -> bool:
        return self.state.remove("library", arxiv_id)

    def refresh(self, previous: dict[str, Any], item: dict[str, Any]) -> bool:
        return self.state.refresh_saved(previous, item)
