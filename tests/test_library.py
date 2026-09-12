from pathlib import Path

from arxiv_ra.library import PaperLibraryStore


def _item() -> dict:
    return {
        "paper": {
            "arxiv_id": "2407.05600",
            "title": "GenArtist",
            "abstract": "Agentic image generation.",
        },
        "verified": {"venue": "NeurIPS 2024"},
    }


def test_library_is_profile_scoped_and_idempotent(tmp_path: Path) -> None:
    first = PaperLibraryStore(tmp_path, "agentict2i")
    second = PaperLibraryStore(tmp_path, "robotics")

    created = first.add(_item(), "AgenticT2I", "2026-08-22")
    repeated = first.add(_item(), "AgenticT2I", "2026-08-22")

    assert created["saved_at"] == repeated["saved_at"]
    assert first.contains("2407.05600") is True
    assert second.contains("2407.05600") is False
    assert len(first.all()) == 1


def test_library_remove_is_safe_to_repeat(tmp_path: Path) -> None:
    store = PaperLibraryStore(tmp_path, "agentict2i")
    store.add(_item(), "AgenticT2I")

    assert store.remove("2407.05600") is True
    assert store.remove("2407.05600") is False
    assert store.all() == {}
