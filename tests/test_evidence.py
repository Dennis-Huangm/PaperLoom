from types import SimpleNamespace
from unittest.mock import Mock

import fitz

from arxiv_ra.config import PDFConfig, AppConfig
from arxiv_ra.evidence import attach_evidence
from arxiv_ra.models import ParsedPaper, Paper, VerifiedMetadata
from arxiv_ra.pdf_pipeline import PDFParser
from arxiv_ra.pipeline import DailyPipeline
from arxiv_ra.report import ReportGenerator
from arxiv_ra.render import markdown_with_math
from arxiv_ra.utils import read_json


QUOTE = "Our method improves accuracy by ten percent on the test benchmark."


def test_valid_quote_uses_real_page_and_reports_partial_coverage():
    parsed = ParsedPaper("", ["Introduction", QUOTE], total_pages=4)
    result, evidence = attach_evidence(f"# Test\n\n## 关键结果\n提高效果。[[证据:{QUOTE}]]", parsed,
                                       pdf_available=True, full_report=True)
    assert "paper.pdf#page=2" in result
    assert evidence["cited_pages"] == [2]
    assert evidence["covered_sections"] == ["关键结果"]
    assert "核心方法" in evidence["missing_sections"]
    assert "部分页面" in result
    assert "paper.pdf#page=2" in markdown_with_math(result)[0]
    assert "[1](paper.pdf#page=2" in result
    assert "PDF 第 2 页" not in markdown_with_math(result.split("## 引用与核对")[0])[0].split("</a>")[0].split(">")[-1]


def test_missing_source_results_table_is_visible_in_coverage():
    pages = ["Table 3: Main benchmark results.\nModel A 91.2", "Table 4: Visual quality results.\nModel A 4.2"]
    report = "# Paper\n\n## 关键结果\n\n### Table 4 视觉质量\n\n| Model | Score |\n|---|---|\n| A | 4.2 |\n"
    result, evidence = attach_evidence(report, ParsedPaper("", pages), pdf_available=True, full_report=True)
    assert evidence["table_coverage"]["source_count"] == 2
    assert evidence["table_coverage"]["presented"] == [4]
    assert [item["number"] for item in evidence["table_coverage"]["missing"]] == [3]
    assert "[Table 3](paper.pdf#page=1)" in result

    with_crop = report.replace("## 关键结果", "## 关键结果\n\n![原文 Table 3](source-table-03.png)")
    _, cropped = attach_evidence(with_crop, ParsedPaper("", pages), pdf_available=True, full_report=True)
    assert cropped["table_coverage"]["presented"] == [4]

    with_table = report.replace("### Table 4 视觉质量", "### Table 3 主结果\n\n| Model | Score |\n|---|---|\n| A | 91.2 |\n\n### Table 4 视觉质量")
    _, reproduced = attach_evidence(with_table, ParsedPaper("", pages), pdf_available=True, full_report=True)
    assert reproduced["table_coverage"]["presented"] == [3, 4]

    partial = with_table.replace("### Table 3 主结果", "### Table 3 主结果节选")
    _, excerpt = attach_evidence(partial, ParsedPaper("", pages), pdf_available=True, full_report=True)
    assert excerpt["table_coverage"]["presented"] == [4]


def test_fake_ambiguous_short_or_unavailable_quotes_never_get_links():
    for pages, quote in [([QUOTE], "An invented result with no source evidence."), ([QUOTE, QUOTE], QUOTE), ([QUOTE], "method")]:
        report, data = attach_evidence(f"[[证据:{quote}]]", ParsedPaper("", pages), pdf_available=True, full_report=True)
        assert "paper.pdf#page=" not in report and data["rejected_citations"] == 1
    for available, full in [(False, True), (True, False)]:
        report, data = attach_evidence(f"[[证据:{QUOTE}]]", ParsedPaper("", [QUOTE]), pdf_available=available, full_report=full)
        assert not data["citations"] and data["status"] == "unavailable"
        assert "paper.pdf#page=" not in report


def test_repeated_quotes_are_deduplicated_and_raw_model_page_links_unverified():
    report = f"## 核心方法\n[[证据:{QUOTE}]]\n## 关键结果\n[[证据:{QUOTE}]]\n[原文](paper.pdf#page=99)"
    result, data = attach_evidence(report, ParsedPaper("", [QUOTE]), pdf_available=True, full_report=True)
    assert len(data["citations"]) == 1
    assert set(data["covered_sections"]) == {"核心方法", "关键结果"}
    assert "page=99" not in result


def test_raw_and_extensionless_model_page_links_are_not_trusted():
    report = '[source](https://arxiv.org/pdf/2407.05600v1#page=9)\n<a href="paper.pdf#page=8">page 8</a>'
    result, data = attach_evidence(report, ParsedPaper("", [QUOTE]), pdf_available=True, full_report=True)
    assert "#page=" not in result and not data["citations"]


def test_quotes_are_retained_in_data_without_repeating_source_in_report():
    quote = 'An example with <img src=x onerror=alert(1)> embedded in the source text.'
    report, data = attach_evidence(f"[[证据:{quote}]]", ParsedPaper("", [quote]), pdf_available=True, full_report=True)
    body = markdown_with_math(report)[0]
    assert "<img" not in body and "&lt;img" not in body
    assert data["citations"][0]["quote"] == quote
    assert 'href="paper.pdf#page=1"' in body and 'href="evidence.json"' in body


def test_numeric_warning_does_not_copy_formatted_claim_into_appendix():
    claim = "### 1. 质量排名\n\n**评测分析**：结果为 13.9%。"
    report, data = attach_evidence("# Paper\n\n## 关键结果\n\n" + claim,
                                  ParsedPaper("", [QUOTE]), pdf_available=True, full_report=True)
    assert "13.9%" in report and "**[待核对]**" in report
    assert "### 1. 质量排名" in report
    assert data["numeric_audit"]["issues"][0]["original"] == "**评测分析**：结果为 13.9%。"
    assert "实验数值待核对" in report and "原陈述" not in report
    assert data["numeric_audit"]["issues"][0]["numbers"] == ["13.9%"]
    assert "评测分析" in data["numeric_audit"]["issues"][0]["claim"]


def test_numeric_table_rows_need_their_own_single_page_exact_evidence():
    header = "Model CLIP DINOv2 MSE Invalid"
    baseline = "no edit 0.9634 0.9011 10488 0"
    model = "GPT-4o mini 0.9040 0.8058 8526 14"
    parsed = ParsedPaper("", [header + "\n" + baseline + "\n" + model])
    report = ("## 关键结果\n\n| 模型 | MSE | 原文依据 |\n| --- | --- | --- |\n"
              f"| no edit | 10488 | [[证据:{baseline}]] |\n"
              f"| GPT-4o mini | 8526 | [[证据:{model}]] |\n"
              f"| wrong | 8527 | [[证据:{model}]] |\n"
              "| missing | 10488 | |\n")
    _, evidence = attach_evidence(report, parsed, pdf_available=True, full_report=True)
    assert evidence["validated_citations"] == 2
    assert [issue["numbers"] for issue in evidence["numeric_audit"]["issues"]] == [["8527"], ["10488"]]
    assert [issue["reason"] for issue in evidence["numeric_audit"]["issues"]] == ["not_in_quote", "no_located_quote"]

    # A real but cross-page quote cannot yield a fabricated page link.
    split = ParsedPaper("", [header + " " + baseline, model])
    _, evidence = attach_evidence(f"[[证据:{baseline} {model}]]", split,
                                  pdf_available=True, full_report=True)
    assert evidence["rejected_citations"] == 1 and not evidence["citations"]


def test_table_after_prose_or_nested_in_list_cannot_borrow_another_rows_quote():
    quote = 'Model A accuracy is 91.2% on the held-out benchmark.'
    parsed = ParsedPaper('', [quote])
    for intro, indent in [('实验结果如下：', ''), ('- **结果汇总**：', '  '), ('### 1. 结果', '')]:
        raw = ('## 关键结果\n\n' + intro + '\n' + indent + '| 模型 | 得分 | 依据 |\n'
               + indent + '| --- | --- | --- |\n'
               + indent + f'| A | 91.2% | [[证据:{quote}]] |\n'
               + indent + '| B | 91.2% | |\n')
        _, evidence = attach_evidence(raw, parsed, pdf_available=True, full_report=True)
        issues = evidence['numeric_audit']['issues']
        assert len(issues) == 1
        assert issues[0]['reason'] == 'no_located_quote' and '| B |' in issues[0]['claim']


def test_table_audit_preserves_multiline_quote_with_blank_lines_and_pipes():
    quote = 'Model A benchmark results:\n\n| test accuracy 91.2% |'
    raw = ('## 关键结果\n\n结果如下：\n| 模型 | 得分 | 依据 |\n| --- | --- | --- |\n'
           f'| A | 91.2% | [[证据:{quote}]] |\n| B | 91.2% | |')
    _, evidence = attach_evidence(raw, ParsedPaper('', [quote]), pdf_available=True, full_report=True)
    assert evidence['validated_citations'] == 1
    assert evidence['numeric_audit']['checked_claims'] == 2
    assert len(evidence['numeric_audit']['issues']) == 1
    assert '| B |' in evidence['numeric_audit']['issues'][0]['claim']


def test_real_pdf_parser_and_pipeline_store_validated_evidence(tmp_path):
    source = tmp_path / "source.pdf"
    with fitz.open() as pdf:
        pdf.new_page().insert_text((50, 50), "Introduction to the test paper.")
        pdf.new_page().insert_text((50, 50), QUOTE, fontsize=9)
        pdf.save(source)
    config = AppConfig(output_dir=str(tmp_path / "run"), profile_id="a")
    config.obsidian.enabled = False
    config.pdf = PDFConfig(parser="pymupdf", max_pages=2, use_docling_if_available=False)
    llm = SimpleNamespace(enabled=True, chat=Mock(return_value=f"# Test\n\n## 关键结果\n结果。[[证据:{QUOTE}]]"))
    parser = PDFParser(config.pdf)
    clients = SimpleNamespace(llm=llm, reporter=ReportGenerator(llm, config.llm), parser=parser,
        verifier=SimpleNamespace(verify=lambda p: VerifiedMetadata(title=p.title)), arxiv_html=SimpleNamespace(fetch=lambda *a: []))
    paper = Paper("2407.05600", "Test", [], "Abstract", [], "", None, None, "", "", version=2)
    artifact = DailyPipeline(config, tmp_path, clients=clients)._process_paper(paper, tmp_path / "run/2026-09-24", False, local_pdf=source)
    evidence = read_json(artifact.report_path.with_name("evidence.json"))
    metadata = read_json(artifact.report_path.with_name("metadata.json"))
    assert evidence["total_pages"] == 2 and evidence["cited_pages"] == [2]
    assert metadata["evidence"]["validated_citations"] == 1 and metadata["paper"]["version"] == 2
    assert artifact.report_path.with_name("paper.pdf").read_bytes() == source.read_bytes()
    assert "[[证据:" in llm.chat.call_args_list[0].args[0]
