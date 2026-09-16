from datetime import datetime, timezone
from pathlib import Path

from arxiv_ra.config import AppConfig, VersionTrackingConfig, ZoteroConfig
from arxiv_ra.models import Author, Paper
from arxiv_ra.utils import read_json, write_json
from arxiv_ra.version_tracker import VersionTracker


def _paper(version: int) -> Paper:
    return Paper(
        arxiv_id="2407.05600",
        title="GenArtist",
        authors=[Author("Researcher")],
        abstract="",
        categories=["cs.CV"],
        primary_category="cs.CV",
        published=datetime(2024, 7, 8, tzinfo=timezone.utc),
        updated=datetime(2026, 8, 19, tzinfo=timezone.utc),
        abs_url=f"https://arxiv.org/abs/2407.05600v{version}",
        pdf_url=f"https://arxiv.org/pdf/2407.05600v{version}",
        version=version,
    )


def _tracker(tmp_path: Path) -> VersionTracker:
    output = tmp_path / "run"
    report_dir = output / "2026-08-17" / "reports" / "2407.05600-genartist"
    write_json(
        report_dir / "metadata.json",
        {"paper": {"arxiv_id": "2407.05600", "title": "GenArtist"}},
    )
    config = AppConfig(
        output_dir=str(output),
        profile_id="agentict2i",
        profile_name="AgenticT2I",
        zotero=ZoteroConfig(enabled=False),
        version_tracking=VersionTrackingConfig(
            include_reports=True,
            include_feedback=False,
            include_zotero=False,
        ),
    )
    return VersionTracker(config, tmp_path)


def test_first_version_check_establishes_baseline_without_event(tmp_path: Path, monkeypatch) -> None:
    tracker = _tracker(tmp_path)
    monkeypatch.setattr(tracker.clients.arxiv, "get_many", lambda ids: [_paper(2)])

    overview = tracker.check(datetime(2026, 8, 19, tzinfo=timezone.utc))

    state = read_json(tracker.state_path, {})
    assert overview.exists()
    assert state["items"]["2407.05600"]["latest_version"] == 2
    assert state["items"]["2407.05600"]["events"] == []


def test_new_version_creates_event(tmp_path: Path, monkeypatch) -> None:
    tracker = _tracker(tmp_path)
    write_json(
        tracker.state_path,
        {
            "items": {
                "2407.05600": {
                    "arxiv_id": "2407.05600",
                    "title": "GenArtist",
                    "latest_version": 1,
                    "events": [],
                }
            }
        },
    )
    monkeypatch.setattr(tracker.clients.arxiv, "get_many", lambda ids: [_paper(2)])
    diff = tracker.output_root / "versions" / "2407.05600" / "v1-to-v2" / "report.html"
    diff.parent.mkdir(parents=True)
    diff.write_text("report", encoding="utf-8")
    monkeypatch.setattr(
        tracker,
        "_version_event",
        lambda paper, old, now: {
            "detected_at": now.isoformat(),
            "from_version": old,
            "to_version": paper.version,
            "report_path": str(diff),
        },
    )

    tracker.check(datetime(2026, 8, 19, tzinfo=timezone.utc))

    state = read_json(tracker.state_path, {})
    assert state["items"]["2407.05600"]["latest_version"] == 2
    assert state["items"]["2407.05600"]["events"][0]["from_version"] == 1
