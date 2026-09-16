from datetime import datetime, timezone
from pathlib import Path

from arxiv_ra.feedback import FeedbackStore
from arxiv_ra.models import Author, Paper


def _paper(arxiv_id: str, title: str, abstract: str = "") -> Paper:
    now = datetime.now(timezone.utc)
    return Paper(
        arxiv_id=arxiv_id,
        title=title,
        authors=[Author("Researcher")],
        abstract=abstract,
        categories=["cs.CV"],
        primary_category="cs.CV",
        published=now,
        updated=now,
        abs_url=f"https://arxiv.org/abs/{arxiv_id}",
        pdf_url=f"https://arxiv.org/pdf/{arxiv_id}",
    )


def test_feedback_is_profile_scoped_and_overwritable(tmp_path: Path) -> None:
    first = FeedbackStore(tmp_path, "agentict2i")
    second = FeedbackStore(tmp_path, "robotics")
    snapshot = {
        "arxiv_id": "2401.00001",
        "title": "Agentic Image Generation with Visual Feedback",
        "abstract": "",
        "primary_category": "cs.CV",
        "abs_url": "https://arxiv.org/abs/2401.00001",
    }

    first.set(snapshot, "not_relevant")

    assert first.all()["2401.00001"]["verdict"] == "not_relevant"
    assert second.all() == {}
    assert (tmp_path / "reading-state-agentict2i.json").exists()
    assert first.remove("2401.00001") is True
    assert first.all() == {}


def test_feedback_terms_raise_related_papers_and_lower_unrelated_pattern(tmp_path: Path) -> None:
    store = FeedbackStore(tmp_path, "agentict2i")
    store.set(
        {"arxiv_id": "2", "title": "Medical Image Segmentation", "abstract": ""},
        "not_relevant",
    )
    library = {
        "1": {
            "saved_at": "2026-08-24T00:00:00+00:00",
            "paper": {"arxiv_id": "1", "title": "Agentic Image Planning"},
        }
    }
    related = _paper("3", "Agentic Image Planning for Editing")
    negative = _paper("4", "Medical Image Segmentation Benchmark")

    store.apply([related, negative], library)

    assert related.feedback_score > 0
    assert negative.feedback_score < 0
    assert related.final_score > negative.final_score
    assert store.blocks(negative) is True
    assert store.blocks(_paper("5", "Medical Robotics for Surgery")) is False
