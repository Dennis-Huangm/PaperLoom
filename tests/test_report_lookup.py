"""Report selection contracts through the existing reading and catalog interfaces."""
import os
from datetime import datetime, timezone

import pytest

from arxiv_ra.report_store import matching_report
from arxiv_ra.utils import write_json
from arxiv_ra.web_catalog import report_library, preferred_report_index, grouped_report_library


def add_report(root, name, *, profile="alpha", version=2, quality="full",
               generated="2026-09-01T12:00:00+00:00", mtime=1000,
               html=True, markdown=True, date="2026-09-01"):
    folder = root / date / "reports" / name
    write_json(folder / "metadata.json", {
        "profile_id": profile, "report_quality": quality, "generated_at": generated,
        "paper": {"arxiv_id": "2609.00001", "version": version, "title": name},
    })
    for suffix, enabled in (("html", html), ("md", markdown)):
        if enabled:
            path = folder / f"report.{suffix}"
            path.write_text(name, encoding="utf-8")
            os.utime(path, (mtime, mtime))
    return folder


def test_web_evidence_and_overview_keep_their_own_tie_breakers(tmp_path):
    generated_later = add_report(tmp_path, "generated-later", generated="2026-09-01T15:00:00+00:00")
    edited_later = add_report(tmp_path, "edited-later", mtime=2000)
    paper = {"arxiv_id": "2609.00001", "version": 2}

    reports = report_library(tmp_path)
    assert preferred_report_index(reports, "alpha")[("2609.00001", 2)]["report_path"] == generated_later / "report.html"
    assert matching_report(tmp_path, "alpha", paper) == edited_later / "report.md"
    assert grouped_report_library(reports)[0]["report_path"] == edited_later / "report.html"


@pytest.mark.parametrize("profile", [None, "", "alpha"])
def test_material_requirements_and_new_operations_remain_independent(tmp_path, profile):
    html_only = add_report(tmp_path, "html-only", profile=profile, markdown=False)
    markdown_only = add_report(tmp_path, "markdown-only", profile=profile, html=False)
    paper = {"arxiv_id": "2609.00001", "version": 2}
    assert matching_report(tmp_path, "alpha", paper) == markdown_only / "report.md"
    assert preferred_report_index(report_library(tmp_path), "alpha")[("2609.00001", 2)]["report_path"] == html_only / "report.html"

    (html_only / "report.html").unlink()
    (markdown_only / "report.md").unlink()
    assert report_library(tmp_path) == []
    assert matching_report(tmp_path, "alpha", paper) is None

    fresh = add_report(tmp_path, "new-operation", profile=profile)
    assert matching_report(tmp_path, "alpha", paper) == fresh / "report.md"
    assert preferred_report_index(report_library(tmp_path), "alpha")[("2609.00001", 2)]["report_path"] == fresh / "report.html"


def test_profile_specificity_precedes_quality_and_never_crosses_revision(tmp_path):
    add_report(tmp_path, "legacy-full", profile="", mtime=3000)
    scoped = add_report(tmp_path, "scoped-abstract", quality="abstract")
    add_report(tmp_path, "other-direction", profile="beta", mtime=4000)
    latest = add_report(tmp_path, "new-revision", version=3, mtime=5000)
    paper = {"arxiv_id": "2609.00001", "version": 2}
    assert matching_report(tmp_path, "alpha", paper) == scoped / "report.md"
    assert preferred_report_index(report_library(tmp_path), "alpha")[("2609.00001", 2)]["report_path"] == scoped / "report.html"
    assert matching_report(tmp_path, "alpha", {"arxiv_id": "2609.00001"}) is None
    assert matching_report(tmp_path, "alpha", {**paper, "version": 1}) is None
    assert grouped_report_library(report_library(tmp_path))[0]["report_path"] == latest / "report.html"

    full = add_report(tmp_path, "scoped-full", mtime=500)
    assert matching_report(tmp_path, "alpha", paper) == full / "report.md"
    assert preferred_report_index(report_library(tmp_path), "alpha")[("2609.00001", 2)]["report_path"] == full / "report.html"


@pytest.mark.parametrize("generated", ["", "2026-09-01T12:00:00", "2026-09-01T14:00:00+02:00"])
def test_evidence_cutoff_preserves_date_fallback_and_timezone(tmp_path, generated):
    included = add_report(tmp_path, "included", generated=generated)
    add_report(tmp_path, "future", generated="2026-09-01T12:00:01+00:00", mtime=3000)
    add_report(tmp_path, "future-date", date="2026-09-02", generated="", mtime=4000)
    add_report(tmp_path, "invalid-time", generated="not-a-date", mtime=5000)
    assert matching_report(tmp_path, "alpha", {"arxiv_id": "2609.00001", "version": 2},
                           before=datetime(2026, 9, 1, 12, tzinfo=timezone.utc)) == included / "report.md"


def test_damaged_metadata_does_not_hide_valid_reports(tmp_path):
    valid = add_report(tmp_path, "valid")
    invalid = add_report(tmp_path, "invalid")
    (invalid / "metadata.json").write_text("{broken", encoding="utf-8")
    non_object = add_report(tmp_path, "non-object")
    write_json(non_object / "metadata.json", ["not report metadata"])
    reports = report_library(tmp_path)
    assert {r["report_path"] for r in reports} == {valid / "report.html", invalid / "report.html"}
    assert next(r for r in reports if r["report_path"] == invalid / "report.html")["arxiv_id"] == ""
    assert matching_report(tmp_path, "alpha", {"arxiv_id": "2609.00001", "version": 2}) == valid / "report.md"


def test_default_direction_and_unknown_revision_keep_legacy_semantics(tmp_path):
    add_report(tmp_path, "legacy", profile=None, version=None, mtime=3000)
    unknown = add_report(tmp_path, "default", profile="default", version=None)
    add_report(tmp_path, "known-version", profile="default", version=2, mtime=4000)
    assert matching_report(tmp_path, "", {"arxiv_id": "2609.00001"}) == unknown / "report.md"
    assert preferred_report_index(report_library(tmp_path), "")[("2609.00001", None)]["report_path"] == unknown / "report.html"


@pytest.mark.parametrize("payload", [None, [], False])
def test_empty_legacy_catalog_metadata_retains_placeholder(tmp_path, payload):
    folder = add_report(tmp_path, "legacy-empty")
    write_json(folder / "metadata.json", payload)
    reports = report_library(tmp_path)
    assert len(reports) == 1
    assert reports[0]["title"] == "legacy-empty"
    assert reports[0]["arxiv_id"] == ""
    assert matching_report(tmp_path, "alpha", {"arxiv_id": "2609.00001", "version": 2}) is None


def test_invalid_encoding_retains_existing_reading_and_catalog_error_policies(tmp_path):
    valid = add_report(tmp_path, "valid")
    invalid = add_report(tmp_path, "invalid")
    (invalid / "metadata.json").write_bytes(b"\xff")
    assert matching_report(tmp_path, "alpha", {"arxiv_id": "2609.00001", "version": 2}) == valid / "report.md"
    with pytest.raises(UnicodeDecodeError):
        report_library(tmp_path)
