from types import SimpleNamespace
from unittest.mock import Mock
import json

import pytest
from bs4 import BeautifulSoup
from fastapi.testclient import TestClient

from arxiv_ra.cli import build_parser
from arxiv_ra.comparison import ComparisonService, comparison_library, DIMENSIONS
from arxiv_ra.config import AppConfig
from arxiv_ra.library import PaperLibraryStore
from arxiv_ra.profiles import ProfileManager
from arxiv_ra.utils import write_json, read_json
from arxiv_ra.web import create_app


def paper(aid="2407.05600", version=1):
    return {"arxiv_id": aid, "title": f"Paper {aid}", "version": version, "abstract": "Public abstract with reported methods", "primary_category": "cs.AI"}


def add_report(root, profile, value, text, quality="full", name="source"):
    path = root / f"2026-09-24/reports/{profile}-{name}-v{value['version']}/report.md"
    write_json(path.with_name("metadata.json"), {"profile_id": profile, "paper": value, "report_quality": quality})
    path.write_text(text, encoding="utf-8")
    path.with_suffix(".html").write_text("<p>Source</p>")
    path.with_name("paper.pdf").write_bytes(b"Test fixture")
    write_json(path.with_name("evidence.json"), {"status": "checked", "citations": [
        {"id": 1, "page": 2, "quote": "An exact source quote for testing references."}]})
    return path


@pytest.fixture
def service(tmp_path):
    for aid in ("2407.05600", "2407.05601"):
        PaperLibraryStore(tmp_path, "a").add({"paper": paper(aid)}, "A")
    cfg = AppConfig(output_dir=str(tmp_path), profile_id="a")
    return ComparisonService(cfg, tmp_path, clients=SimpleNamespace(llm=SimpleNamespace(enabled=False)))


@pytest.mark.parametrize("keys", [["2407.05600v1"], ["2407.05600v1"] * 2, ["unknownv1", "2407.05600v1"], [str(i) for i in range(6)]])
def test_selection_rejects_invalid_number_or_unavailable_versions(service, keys):
    with pytest.raises(ValueError):
        service.prepare(keys)


def test_same_paper_two_revisions_is_not_cross_paper_comparison(service, tmp_path):
    add_report(tmp_path, "a", paper(version=2), "New version")
    with pytest.raises(ValueError, match="不同论文"):
        service.prepare(["2407.05600v1", "2407.05600v2"])


def test_comparison_freezes_exact_profile_revision_and_preferred_quality(service, tmp_path):
    correct = add_report(tmp_path, "a", paper(), "## 核心方法\nRIGHT FULL")
    add_report(tmp_path, "a", paper(), "WRONG FALLBACK", "abstract", "fallback")
    add_report(tmp_path, "a", paper(version=9), "WRONG REVISION", name="new")
    add_report(tmp_path, "b", paper(), "WRONG PROFILE")
    snapshot = service.prepare(["2407.05600v1", "2407.05601v1"])
    correct.write_text("REPLACED AFTER SUBMISSION")
    result = service.generate(snapshot)
    saved = read_json(result.parent / "sources.json")
    assert "RIGHT FULL" in str(saved)
    assert all(text not in str(saved) for text in ["WRONG FALLBACK", "WRONG PROFILE", "WRONG REVISION", "REPLACED AFTER SUBMISSION"])
    assert saved["sources"][0]["report_sha256"]
    assert saved["sources"][1]["quality"] == "metadata"
    assert all(x["paper"]["version"] == 1 for x in saved["sources"])
    assert len(comparison_library(tmp_path, "a")) == 1 and comparison_library(tmp_path, "b") == []


def test_matrix_checks_source_ownership_renders_table_and_inference_label(service):
    snapshot = service.prepare(["2407.05600v1", "2407.05601v1"])
    response = {"papers": [{"id": "P1", "dimensions": {
        "核心方法": {"text": "Method A | <script>unsafe()</script>", "kind": "inference", "evidence": ["P1:A"],
                 "support": [{"evidence": "P1:A", "quote": "Public abstract with reported methods"}]},
        "关键结果": {"text": "Incorrect borrowed number", "kind": "source", "evidence": ["P2:A"]}}}]}
    service.clients.llm = SimpleNamespace(enabled=True, chat=Mock(return_value=json.dumps(response)))
    result = service.generate(snapshot)
    matrix = read_json(result.parent / "matrix.json")
    assert matrix["P1"]["核心方法"]["kind"] == "inference"
    assert matrix["P1"]["关键结果"]["kind"] == "unknown"
    text = result.read_text(encoding="utf-8")
    tree = BeautifulSoup(text, "html.parser")
    assert len(tree.select("table tbody tr")) == len(DIMENSIONS)
    assert all(len(row.select("td")) == 3 for row in tree.select("table tbody tr"))
    assert "Incorrect borrowed number" not in text and "<script>unsafe()" not in text
    assert "推断" in text and tree.select_one('a[href="https://arxiv.org/abs/2407.05600v1"]')
    assert "固定证据摘录" not in text


def test_model_failure_preserves_previous_attempt_and_excludes_notes(service, tmp_path):
    store = PaperLibraryStore(tmp_path, "a").state
    store.update_reading("2407.05600", status="read", notes="PRIVATE_NOTE", tags=["PRIVATE_TAG"], read_version=1, expected_updated_at="")
    snapshot = service.prepare(["2407.05600v1", "2407.05601v1"])
    assert "PRIVATE_NOTE" not in str(snapshot) and "PRIVATE_TAG" not in str(snapshot)
    first = service.generate(snapshot)
    old = first.read_bytes()
    service.clients.llm = SimpleNamespace(enabled=True, chat=Mock(side_effect=RuntimeError("offline")))
    second = service.generate(snapshot)
    assert first != second and first.read_bytes() == old
    assert read_json(second.parent / "metadata.json")["status"] == "fallback"


def test_api_rejects_old_direction_and_captures_sources_before_queue(tmp_path, monkeypatch):
    config = tmp_path / "config.yaml"
    config.write_text("output_dir: run\ndiscovery:\n  interest_description: alpha\n", encoding="utf-8")
    app = create_app(config)
    for aid in ("2407.05600", "2407.05601"):
        PaperLibraryStore(tmp_path / "run", "alpha").add({"paper": paper(aid)}, "Alpha")
    captured = []
    def fake_generate(self, snapshot):
        captured.append(snapshot)
        path = tmp_path / "run/comparisons/test/report.html"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("result")
        return path
    monkeypatch.setattr(ComparisonService, "generate", fake_generate)
    with TestClient(app) as client:
        assert "跨论文比较" in client.get("/compare").text
        assert "研究活动周报" in client.get("/weekly").text
        assert client.post("/api/jobs/compare", data={"papers": ["2407.05600v1"], "profile_id": "alpha"}).status_code == 400
        response = client.post("/api/jobs/compare", data={"papers": ["2407.05600v1", "2407.05601v1"], "profile_id": "alpha"})
        assert response.status_code == 202
        profiles = ProfileManager(tmp_path)
        profiles.save({"id": "beta", "name": "Beta"}); profiles.activate("beta")
        assert client.post("/api/jobs/compare", data={"papers": ["2407.05600v1", "2407.05601v1"], "profile_id": "alpha"}).status_code == 409
    assert captured and captured[0]["profile_id"] == "alpha"


def test_cli_has_explicit_note_opt_in_and_pinned_compare_inputs():
    parser = build_parser()
    assert not parser.parse_args(["weekly"]).include_notes
    assert parser.parse_args(["weekly", "--include-notes"]).include_notes
    args = parser.parse_args(["compare", "2407.05600v1", "2407.05601v2", "--question", "成本"])
    assert len(args.papers) == 2 and args.question == "成本"
