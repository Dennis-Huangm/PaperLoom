from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import Mock
from zoneinfo import ZoneInfo

from arxiv_ra.activity import activity_markdown, collect_activity, literal
from arxiv_ra.render import markdown_with_math
from bs4 import BeautifulSoup
from arxiv_ra.config import AppConfig, WeeklyConfig
from arxiv_ra.library import PaperLibraryStore
from arxiv_ra.utils import read_json, write_json
from arxiv_ra.weekly import WeeklySynthesizer


def paper(aid="2407.05600", version=1):
    return {"arxiv_id": aid, "version": version, "title": "Active research paper", "abstract": f"Public abstract v{version}",
            "primary_category": "cs.AI", "abs_url": f"https://arxiv.org/abs/{aid}v{version}"}


def test_journal_retains_reading_and_notes_after_removal_without_duplicate_saves(tmp_path):
    library = PaperLibraryStore(tmp_path, "a")
    library.add({"paper": paper()}, "A")
    library.add({"paper": paper()}, "A")
    record = library.state.update_reading("2407.05600", status="read", notes="Private conclusion", tags=["private"],
                                        read_version=1, expected_updated_at="")
    library.state.update_reading("2407.05600", status="read", notes="Private conclusion", tags=["private"],
                                 read_version=1, expected_updated_at=record["updated_at"])
    library.add({"paper": paper(version=2)}, "A")
    library.remove("2407.05600")
    result = collect_activity(tmp_path, "a", datetime.now(timezone.utc) + timedelta(seconds=1), 7)
    assert result["counts"] == {"saved": 1, "reading": 1, "notes": 1, "library_version": 1, "removed": 1, "completed": 1}
    assert result["events"][1]["read_version"] == 1
    assert any(e.get("notes") == "Private conclusion" for e in result["events"])
    assert not collect_activity(tmp_path, "b", datetime.now(timezone.utc), 7)["events"]


def test_activity_uses_local_day_boundaries_and_excludes_future_or_other_profile(tmp_path):
    now = datetime(2026, 9, 24, 12, tzinfo=ZoneInfo("Asia/Shanghai"))
    events = [{"kind": "saved", "at": t, "paper": paper(str(i))} for i, t in enumerate([
        "2026-09-17T15:59:59+00:00", "2026-09-17T16:00:00+00:00", "2026-09-24T04:00:00+00:00", "2026-09-24T04:00:01+00:00"])]
    write_json(tmp_path / "reading-state-a.json", {"version": 1, "library": {}, "feedback": {}, "activity": events})
    for profile in ("a", "b"):
        path = tmp_path / f"2026-09-24/reports/{profile}/metadata.json"
        write_json(path, {"profile_id": profile, "generated_at": now.isoformat(), "paper": paper(), "report_quality": "abstract"})
        path.with_name("report.md").write_text("# Source")
    write_json(tmp_path / "version-state-a.json", {"items": {"2407.05600": {"title": "New revision", "events": [
        {"detected_at": now.isoformat(), "from_version": 1, "to_version": 2, "status": "failed"}]}}})
    activity = collect_activity(tmp_path, "a", now, 7)
    assert activity["counts"]["saved"] == 2 and activity["counts"]["report"] == 1
    assert activity["counts"]["version"] == 1
    assert activity["events"][-1]["analysis_status"] == "failed"


def test_legacy_history_is_not_double_counted_or_backdated_with_current_notes(tmp_path):
    now = datetime(2026, 9, 24, tzinfo=timezone.utc)
    write_json(tmp_path / "reading-state-a.json", {"version": 1, "library": {"2407.05600": {
        "paper": paper(), "saved_at": "2026-09-20T00:00:00Z"}}, "feedback": {},
        "activity_started_at": "2026-09-22T00:00:00Z", "activity": [{"kind": "reading", "at": "2026-09-23T00:00:00Z", "paper": paper(), "status": "read"}],
        "reading": {"2407.05600": {"history": [{"at": "2026-09-23T00:00:00Z", "status": "read"}],
            "notes": "Future notes", "updated_at": "2026-09-25T00:00:00Z"}}})
    activity = collect_activity(tmp_path, "a", now, 7)
    assert activity["counts"]["completed"] == 1 and activity["counts"]["saved"] == 1
    assert "Future notes" not in str(activity)


def test_weekly_activity_without_recommendations_notes_opt_in_and_immutable_attempts(tmp_path):
    library = PaperLibraryStore(tmp_path, "a")
    library.add({"paper": paper()}, "A")
    library.state.update_reading("2407.05600", status="read", notes="PRIVATE_NOT_FOR_MODEL <script>bad()</script>",
        tags=["PRIVATE_TAG"], read_version=1, expected_updated_at="")
    llm = SimpleNamespace(enabled=True, chat=Mock(return_value="# Model weekly\n\nPublic analysis"))
    cfg = AppConfig(output_dir=str(tmp_path), profile_id="a", weekly=WeeklyConfig(days=7))
    cfg.obsidian.enabled = False
    service = WeeklySynthesizer(cfg, tmp_path, clients=SimpleNamespace(llm=llm))
    first = service.generate()
    old = first.with_suffix(".md").read_bytes()
    second = service.generate(include_notes=True)
    assert first != second and first.with_suffix(".md").read_bytes() == old
    assert "本期研究活动" in old.decode() and "PRIVATE_NOT_FOR_MODEL" not in old.decode()
    assert "PRIVATE_NOT_FOR_MODEL" in second.read_text(encoding="utf-8")
    assert "<script>bad()" not in second.read_text(encoding="utf-8")
    assert "PRIVATE_NOT_FOR_MODEL" not in str(llm.chat.call_args_list)
    assert "PRIVATE_TAG" not in str(llm.chat.call_args_list)
    assert "PRIVATE_NOT_FOR_MODEL" not in (first.parent / "activity.json").read_text(encoding="utf-8")
    assert read_json(second.parent / "metadata.json")["activity_counts"]["completed"] == 1
    assert read_json(second.parent / "metadata.json")["paper_count"] == 1


def test_reading_old_revision_never_borrows_new_revision_abstract(tmp_path):
    config = AppConfig(output_dir=str(tmp_path), profile_id="a")
    service = WeeklySynthesizer(config, tmp_path, clients=SimpleNamespace())
    activity = {"events": [{"kind": "reading", "at": "2026-09-24T00:00:00Z", "paper": paper(version=3), "read_version": 1}]}
    collected = service._with_activity([{"paper": paper(version=3)}], activity)
    assert collected[0]["paper"]["version"] == 1
    assert "Public abstract v3" not in str(collected)


def test_note_activity_and_appendix_use_read_revision_not_saved_revision(tmp_path):
    library = PaperLibraryStore(tmp_path, "a")
    library.add({"paper": paper(version=3)}, "A")
    library.state.update_reading("2407.05600", status="read", notes="Notes about the older revision",
        tags=[], read_version=1, expected_updated_at="")
    activity = collect_activity(tmp_path, "a", datetime.now(timezone.utc) + timedelta(seconds=1), 7)
    body, _ = markdown_with_math(activity_markdown(activity, include_notes=True))
    tree = BeautifulSoup(body, "html.parser")
    note_event = next(item.get_text() for item in tree.select("li") if "个人笔记更新" in item.get_text())
    assert "v1" in note_event and "v3" not in note_event
    assert "v1" in tree.select_one("h3").get_text()
    assert "v3" in tree.get_text()  # The saved event still describes v3.


def test_literal_notes_and_evidence_do_not_become_math_or_active_links():
    source = r"[原文 1](paper.pdf#page=1) $cost$ \[x\] \(y\) | <script>bad()</script> & &#35;"
    body, _ = markdown_with_math("> " + literal(source))
    tree = BeautifulSoup(body, "html.parser")
    assert not tree.select(".math-inline, .math-block, a, script")
    assert tree.get_text().strip() == source


def test_recovered_weekly_period_keeps_cutoff_but_records_actual_generation_time(tmp_path):
    cutoff = datetime(2020, 1, 2, tzinfo=timezone.utc)
    config = AppConfig(output_dir=str(tmp_path), profile_id="a", timezone="UTC")
    config.obsidian.enabled = False
    service = WeeklySynthesizer(config, tmp_path, clients=SimpleNamespace())
    started = datetime.now(timezone.utc)
    result = service.generate(now=cutoff)
    metadata = read_json(result.parent / "metadata.json")
    assert metadata["period_end"] == cutoff.isoformat()
    assert metadata["week_id"] == "2020-W01"
    assert datetime.fromisoformat(metadata["generated_at"]) >= started
