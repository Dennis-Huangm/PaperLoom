from concurrent.futures import ThreadPoolExecutor, ProcessPoolExecutor
import multiprocessing

import pytest

from arxiv_ra.feedback import FeedbackStore
from arxiv_ra.library import PaperLibraryStore
from arxiv_ra.reading_state import ReadingStateStore
from arxiv_ra.utils import write_json


def _save(root, aid):
    PaperLibraryStore(root, "test").add({"paper": {"arxiv_id": aid}}, "Test")


@pytest.mark.parametrize("processes", [False, True])
def test_concurrent_writes_from_independent_instances_are_preserved(tmp_path, processes):
    executor = (ProcessPoolExecutor(max_workers=3, mp_context=multiprocessing.get_context("spawn"))
                if processes else ThreadPoolExecutor(max_workers=8))
    with executor:
        futures = [executor.submit(_save, tmp_path, str(i)) for i in range(24)]
        for future in futures:
            future.result(timeout=30)
    assert set(PaperLibraryStore(tmp_path, "test").all()) == {str(i) for i in range(24)}


def test_failed_transition_preserves_previous_state(tmp_path, monkeypatch):
    library = PaperLibraryStore(tmp_path, "test")
    _save(tmp_path, "1")
    previous = library.all()

    def fail(*args):
        raise OSError("disk full")

    with monkeypatch.context() as patch:
        patch.setattr("arxiv_ra.reading_state.write_json", fail)
        with pytest.raises(OSError):
            FeedbackStore(tmp_path, "test").set({"arxiv_id": "1"}, "not_relevant")
    assert library.all() == previous
    assert FeedbackStore(tmp_path, "test").all() == {}


def test_mutual_exclusion_and_repair_cannot_resurrect_dismissed_paper(tmp_path):
    library = PaperLibraryStore(tmp_path, "test")
    feedback = FeedbackStore(tmp_path, "test")
    _save(tmp_path, "1")
    previous = library.all()["1"]
    feedback.set({"arxiv_id": "1"}, "not_relevant")
    assert not library.refresh(previous, {"paper": {"arxiv_id": "1", "abstract_zh": "修复"}})
    assert not library.contains("1")
    _save(tmp_path, "1")
    assert not feedback.all()


def test_legacy_files_are_imported_and_retained(tmp_path):
    library = {"1": {"arxiv_id": "1", "saved_at": "2026-01-01", "paper": {"arxiv_id": "1"}}}
    feedback = {"2": {"arxiv_id": "2", "verdict": "not_relevant"}}
    write_json(tmp_path / "paper-library-test.json", library)
    write_json(tmp_path / "feedback-test.json", {"version": 2, "items": feedback})
    old_bytes = (tmp_path / "paper-library-test.json").read_bytes()
    _save(tmp_path, "3")
    state = ReadingStateStore(tmp_path, "test").snapshot()
    assert set(state["library"]) == {"1", "3"}
    assert state["feedback"] == feedback
    assert (tmp_path / "paper-library-test.json").read_bytes() == old_bytes
    assert not ReadingStateStore(tmp_path, "other").snapshot()["library"]


def test_even_number_of_concurrent_toggles_returns_to_unsaved(tmp_path):
    import threading
    start = threading.Barrier(8)
    def toggle():
        start.wait(timeout=5)
        return PaperLibraryStore(tmp_path, "test").toggle({"paper": {"arxiv_id": "1"}}, "Test")
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(toggle) for _ in range(8)]
        results = [future.result(timeout=5) for future in futures]
    assert results.count(True) == results.count(False) == 4
    assert not PaperLibraryStore(tmp_path, "test").contains("1")


def test_legacy_conflicts_keep_newer_action_and_negative_wins_missing_time(tmp_path):
    write_json(tmp_path / "paper-library-test.json", {"1": {"updated_at": "2026-09-16"}, "2": {}})
    write_json(tmp_path / "feedback-test.json", {"1": {"updated_at": "2026-09-15"}, "2": {}})
    state = ReadingStateStore(tmp_path, "test").snapshot()
    assert set(state["library"]) == {"1"} and set(state["feedback"]) == {"2"}
