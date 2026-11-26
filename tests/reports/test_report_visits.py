"""Report history remains independent of generated materials and browser state."""
from concurrent.futures import ThreadPoolExecutor

from paperloom.reports.report_visits import ReportVisitStore, report_visit_key


def test_concurrent_opens_preserve_each_paper_and_share_versions(tmp_path):
    def record(index):
        ReportVisitStore(tmp_path).record(f"2407.{index:05}v2", f"report-{index}")

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(record, range(30)))
    visits = ReportVisitStore(tmp_path).snapshot()
    assert len(visits) == 30
    assert set(visits) == {f"paper:2407.{index:05}" for index in range(30)}
    assert report_visit_key("2407.00001v3", "different-report") == "paper:2407.00001"


def test_unidentified_legacy_reports_do_not_share_history(tmp_path):
    store = ReportVisitStore(tmp_path)
    store.record("", "2026-10-01/reports/one/report.html")
    store.record("", "2026-10-01/reports/two/report.html")
    assert set(store.snapshot()) == {
        "report:2026-10-01/reports/one/report.html",
        "report:2026-10-01/reports/two/report.html",
    }
