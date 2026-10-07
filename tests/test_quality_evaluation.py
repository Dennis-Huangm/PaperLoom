"""Offline quality corpus plus integration checks; never calls a model service."""
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import fitz
import pytest

from arxiv_ra.comparison import ComparisonService
from arxiv_ra.config import AppConfig, PDFConfig
from arxiv_ra.evidence import attach_evidence
from arxiv_ra.models import Paper, ParsedPaper, VerifiedMetadata
from arxiv_ra.pdf_pipeline import PDFParser
from arxiv_ra.pipeline import DailyPipeline
from arxiv_ra.quality import numbers, unsupported_numbers
from arxiv_ra.report import ReportGenerator
from arxiv_ra.task_runtime import TaskHooks, bind_task_hooks
from arxiv_ra.utils import read_json, write_json
from arxiv_ra.library import PaperLibraryStore


ROOT = Path(__file__).resolve().parents[1]
CASES = json.loads((ROOT / "tests/fixtures/quality/cases.json").read_text(encoding="utf-8"))["cases"]
spec = importlib.util.spec_from_file_location("quality_evaluator", ROOT / "tests/quality_evaluator.py")
evaluator = importlib.util.module_from_spec(spec)
spec.loader.exec_module(evaluator)


@pytest.mark.parametrize("case", [case for case in CASES if "expected" in case], ids=lambda case: case["id"])
def test_offline_quality_corpus(case):
    result = evaluator.evaluate(case)
    assert result["passed"], result


def test_semantic_metric_swap_remains_explicit_manual_review():
    case = next(case for case in CASES if "manual_review" in case)
    result = evaluator.evaluate(case)
    assert result["passed"] is None
    assert result["actual"]["kind"] == "source"
    # Deliberately document the limit: matching numbers cannot validate metric ownership.
    assert result["manual_review"]


@pytest.mark.parametrize("claim,support,missing", [
    ("91.2%", "91.20 percent", []),
    ("1,024 samples", "1024 samples", []),
    ("1.2e3", "1200", []),
    ("2 个百分点", "2 percentage points", []),
    ("2百分点", "2 percentage points", []),
    ("12 ms", "12 s", ["12 ms"]),
    ("-1.2", "1.2", ["-1.2"]),
    ("0.912", "91.2%", ["0.912"]),
    ("0.1234567890123456789012345678901", "0.1234567890123456789012345678902", ["0.1234567890123456789012345678901"]),
])
def test_numeric_literals_preserve_units_sign_and_precision(claim, support, missing):
    assert unsupported_numbers(claim, support) == missing


def test_formulas_identifiers_and_references_are_not_experimental_numbers():
    assert not numbers(r"GPT-4, ResNet-50, F1, v2; Figure 3, Table 2; \(L=\sum_{i=1}^n x_i^2\)")
    assert unsupported_numbers(r"\(91.2\%\)", "Accuracy 91.2%") == []


def test_long_document_table_and_physical_pages_remain_auditable(tmp_path):
    pdf = tmp_path / "long.pdf"
    quote = "Table 2 reports Dataset-A test accuracy as 91.2% and F1 as 88.4%."
    with fitz.open() as document:
        for i in range(11):
            document.new_page().insert_text((40, 40), ("Long method discussion. " * 5) + str(i))
        document.new_page().insert_text((40, 40), quote, fontsize=8)
        document.save(pdf)
    parser = PDFParser(PDFConfig(use_docling_if_available=False, max_pages=20))
    parsed = parser.parse(pdf, tmp_path / "parsed")
    report = "# Long paper\n\n## 核心方法\n" + "方法说明。" * 9000
    report += f"\n\n## 关键结果\n| 指标 | 值 |\n|---|---|\n| accuracy | 99.9% [[证据:{quote}]] |"
    result, evidence = attach_evidence(report, parsed, pdf_available=True, full_report=True)
    assert evidence["cited_pages"] == [12]
    assert 'numeric_audit' not in evidence
    assert '待核对' not in result
    assert "paper.pdf#page=12" in result


def test_scan_or_truncated_pdf_never_confirms_unseen_numbers(tmp_path):
    pdf = tmp_path / "scan.pdf"
    with fitz.open() as document:
        document.new_page()  # Blank page represents the no-extractable-text boundary.
        document.new_page().insert_text((40, 40), "The reported accuracy on the held-out test set is 91.2%.")
        document.save(pdf)
    parsed = PDFParser(PDFConfig(use_docling_if_available=False, max_pages=1)).parse(pdf, tmp_path / "parsed")
    report = "## 关键结果\n准确率为 91.2%。[[证据:The reported accuracy on the held-out test set is 91.2%.]]"
    _, evidence = attach_evidence(report, parsed, pdf_available=True, full_report=True)
    assert evidence["status"] == "unavailable"
    assert evidence["total_pages"] == 2 and evidence["validated_citations"] == 0
    assert 'numeric_audit' not in evidence


def snapshot(kind="abstract"):
    return {"profile_id": "alpha", "question": "质量", "prepared_at": "2026-09-25T12:00:00+08:00", "sources": [
        {"id": tag, "key": f"2407.0560{i}v2", "paper": {"arxiv_id": f"2407.0560{i}", "version": 2, "title": "Fixture"},
         "quality": "full", "evidence": [{"id": f"{tag}:A", "kind": kind, "text": "Test accuracy on Dataset-A is 91.2% under the stated protocol."}]}
        for i, tag in enumerate(["P1", "P2"])]}


@pytest.mark.parametrize("support", [None, "wrong", [], [{"evidence":"P1:A","quote":"short"}],
                                   [{"evidence":"P2:A","quote":"Test accuracy on Dataset-A is 91.2%"}],
                                   [{"evidence":"P1:A","quote":"This is a fabricated support excerpt."}],
                                   [{"evidence":[],"quote":"Test accuracy on Dataset-A is 91.2%"}]])
def test_malformed_support_fails_closed(support):
    payload = {"papers": [{"id": "P1", "dimensions": {"关键结果": {"text":"准确率 91.2%", "kind":"source", "evidence":["P1:A"], "support":support}}}]}
    matrix, rejected = ComparisonService._matrix(snapshot(), payload)
    assert rejected == 1 and matrix["P1"]["关键结果"]["kind"] == "unknown"


def test_report_only_numbers_cannot_be_laundered_as_direct_evidence():
    value = snapshot("report")
    cell = {"text":"准确率 91.2%", "kind":"source", "evidence":["P1:A"],
            "support":[{"evidence":"P1:A","quote":value["sources"][0]["evidence"][0]["text"]}]}
    matrix, _ = ComparisonService._matrix(value, {"papers":[{"id":"P1","dimensions":{"关键结果":cell}}]})
    assert matrix["P1"]["关键结果"]["rejection_reason"] == "numeric_without_primary_support"


@pytest.mark.parametrize("payload", [{"papers":None}, {"papers":{}}, {"papers":"bad"}])
def test_malformed_model_result_still_publishes_safe_fallback(tmp_path, payload):
    cfg = AppConfig(output_dir=str(tmp_path), profile_id="alpha")
    llm = SimpleNamespace(enabled=True, chat=Mock(return_value=json.dumps(payload)))
    result = ComparisonService(cfg, tmp_path, clients=SimpleNamespace(llm=llm)).generate(snapshot())
    assert read_json(result.parent / "metadata.json")["status"] == "fallback"
    matrix = read_json(result.parent / "matrix.json")
    assert all(cell["kind"] == "unknown" for row in matrix.values() for cell in row.values())


def test_duplicate_paper_rows_are_not_silently_overwritten():
    value = snapshot()
    cell = {"text":"准确率 91.2%", "kind":"source", "evidence":["P1:A"],
            "support":[{"evidence":"P1:A","quote":value["sources"][0]["evidence"][0]["text"]}]}
    row = {"id":"P1", "dimensions":{"关键结果":cell}}
    matrix, _ = ComparisonService._matrix(value, {"papers":[row, row]})
    assert matrix["P1"]["关键结果"]["rejection_reason"] == "duplicate_paper_rows"


def test_comparison_persists_rejection_reasons_and_exact_support(tmp_path):
    cfg = AppConfig(output_dir=str(tmp_path), profile_id="alpha")
    value = snapshot()
    cell = {"text":"准确率 91.2%", "kind":"source", "evidence":["P1:A"],
            "support":[{"evidence":"P1:A","quote":value["sources"][0]["evidence"][0]["text"]}]}
    payload = {"papers":[{"id":"P1","dimensions":{"关键结果":cell, "复现成本":{**cell,"text":"需要 800 GB 显存"}}}]}
    llm = SimpleNamespace(enabled=True, chat=Mock(return_value=json.dumps(payload)))
    service = ComparisonService(cfg, tmp_path, clients=SimpleNamespace(llm=llm))
    result = service.generate(value)
    matrix = read_json(result.parent / "matrix.json")
    metadata = read_json(result.parent / "metadata.json")
    assert matrix["P1"]["关键结果"]["support"] == cell["support"]
    assert matrix["P1"]["复现成本"]["rejection_reason"] == "numeric_mismatch"
    assert metadata["quality_checks"]["accepted_cells"] == 1
    assert metadata["quality_checks"]["semantic_support"] == "not_assessed"
    output = result.read_text(encoding="utf-8")
    assert "800 GB" not in output and "Test accuracy" not in output
    assert read_json(result.parent / "sources.json") == value
    assert 'href="sources.json"' in output and 'href="matrix.json"' in output
    assert "条目依据与待核对原因" not in output and "数值或单位未见于" not in output
    assert "证据不足" in output
    assert llm.chat.call_count == 1


def test_report_warning_persistence_and_comparison_uses_fixed_revision(tmp_path):
    cfg = AppConfig(output_dir=str(tmp_path / "run"), profile_id="alpha")
    cfg.obsidian.enabled = False
    quote = "The evaluated system obtains 87.5% accuracy on Dataset-A."
    llm = SimpleNamespace(enabled=True, chat=Mock(return_value=f"# Fixture\n\n## 关键结果\n准确率为 91.2%。[[证据:{quote}]]"))
    clients = SimpleNamespace(llm=llm, reporter=ReportGenerator(llm, cfg.llm),
        verifier=SimpleNamespace(verify=lambda p: VerifiedMetadata(title=p.title)),
        parser=SimpleNamespace(parse=lambda *a: ParsedPaper(quote, [quote], total_pages=1)),
        arxiv_html=SimpleNamespace(fetch=lambda *a: []))
    pdf = tmp_path / "fixture.pdf"
    pdf.write_bytes(b"fixture PDF, parser mocked")
    paper = Paper.from_dict({"arxiv_id":"2407.05600", "version":2, "metadata_status":"complete", "title":"Fixture"})
    warnings = []
    with bind_task_hooks(TaskHooks(lambda *a: None, lambda *a: warnings.append(a), lambda: False)):
        artifact = DailyPipeline(cfg, tmp_path, clients=clients)._process_paper(paper, Path(cfg.output_dir) / "2026-09-25", False, local_pdf=pdf)
    metadata = read_json(artifact.report_path.with_name("metadata.json"))
    assert metadata["report_quality"] == "full"  # Execution mode, not a truthfulness grade.
    assert 'numeric_audit' not in metadata['evidence']
    assert "准确率为 91.2%" in artifact.report_path.read_text(encoding="utf-8")
    assert not any(component == "报告数值核对" for component, _ in warnings)
    PaperLibraryStore(Path(cfg.output_dir), "alpha").add({"paper":{"arxiv_id":"2407.05601", "version":1, "title":"Other"}}, "Alpha")
    comparison = ComparisonService(cfg, tmp_path, clients=SimpleNamespace(llm=llm)).prepare(["2407.05600v2", "2407.05601v1"])
    assert comparison["sources"][0]["report_numeric_issues"] == 0
    assert comparison["sources"][0]["key"] == "2407.05600v2"
    assert any("87.5%" in e["text"] for e in comparison["sources"][0]["evidence"] if e["kind"] == "quote")
