"""A missing prose citation is repaired only from the right PDF assertion."""

import json
import hashlib
from arxiv_ra.citation_repair import repair_numeric_citations
from arxiv_ra.config import LLMConfig
from arxiv_ra.evidence import attach_evidence
from arxiv_ra.models import Paper, ParsedPaper, VerifiedMetadata
from arxiv_ra.report import ReportGenerator


def response(*segments):
    return json.dumps({"repairs": [{"index": 0, "segments": [
        {"text": text, "source_ids": ids, "separator": separator}
        for text, ids, separator in segments
    ]}]})


def test_repairs_dropped_prose_citation_from_full_pdf():
    page = (
        "SVGAnim-SFT contains 123k training samples. "
        "The method uses sparse updates and a rendering-aware reward. "
        + "Dataset curation details. " * 20
        + "The remaining 1k samples form a held-out test set."
    )
    report = "## 实验设置\n\n- SVGAnim-SFT 含 123k 样本；剩余 1k 样本作为测试集。"
    parsed = ParsedPaper(page, [page])
    original, before = attach_evidence(report, parsed, pdf_available=True, full_report=True)
    assert len(before["numeric_audit"]["issues"]) == 1
    assert "1k 样本" in original and "**[待核对]**" in original

    def chat(_system, prompt):
        candidates = json.loads(prompt.split("候选原文 JSON：\n", 1)[1])
        training = next(item for item in candidates if "123k" in item["text"])
        test = next(item for item in candidates if "remaining 1k" in item["text"])
        return response(
            ("- SVGAnim-SFT 含 123k 样本", [training["id"]], "；"),
            ("剩余 1k 样本作为测试集", [test["id"]], "。"),
        )

    repaired = repair_numeric_citations(report, parsed, chat)
    output, after = attach_evidence(repaired, parsed, pdf_available=True, full_report=True)
    assert not after["numeric_audit"]["issues"]
    assert "1k 样本作为测试集" in output


def test_same_number_from_wrong_model_is_not_offered_or_accepted():
    page = "Beta model accuracy is 91.2 on the test set. " + "Further evaluation details. " * 10
    report = "## 关键结果\n\n- Alpha model accuracy 为 91.2。"
    parsed = ParsedPaper(page, [page])

    def chat(_system, _prompt):
        raise AssertionError("wrong-owner source must not reach the model")

    assert repair_numeric_citations(report, parsed, chat) == report


def test_invalid_repair_cannot_replace_an_unsupported_claim():
    page = "Alpha model accuracy is 91.2 on the test set. " + "Further evaluation details. " * 10
    report = "## 关键结果\n\n- Alpha model accuracy 为 99.9。"
    parsed = ParsedPaper(page, [page])

    def chat(_system, prompt):
        candidate = json.loads(prompt.split("候选原文 JSON：\n", 1)[1])[0]
        return response(("- Alpha model accuracy 为 99.9", [candidate["id"]], "。"))

    assert repair_numeric_citations(report, parsed, chat) == report


def test_same_number_under_different_metric_is_rejected():
    page = ("Alpha model BLEU is 91.2. Alpha model accuracy is 88.0. "
            + "Further evaluation details. " * 10)
    report = "## 关键结果\n\n- Alpha model accuracy 为 91.2。"
    parsed = ParsedPaper(page, [page])

    def chat(_system, prompt):
        candidate = json.loads(prompt.split("候选原文 JSON：\n", 1)[1])[0]
        return response(("- Alpha model accuracy 为 91.2", [candidate["id"]], "。"))

    assert repair_numeric_citations(report, parsed, chat) == report


def test_same_number_under_different_model_on_same_page_is_rejected():
    page = ("Alpha model accuracy is 88.0. Beta model accuracy is 91.2. "
            + "Further evaluation details. " * 10)
    report = "## 关键结果\n\n- Alpha model accuracy 为 91.2。"
    parsed = ParsedPaper(page, [page])

    def chat(_system, prompt):
        candidate = json.loads(prompt.split("候选原文 JSON：\n", 1)[1])[0]
        return response(("- Alpha model accuracy 为 91.2", [candidate["id"]], "。"))

    assert repair_numeric_citations(report, parsed, chat) == report


def test_chinese_metric_name_must_match_source_metric():
    page = ("Alpha model BLEU is 91.2. Alpha model accuracy is 88.0. "
            + "Further evaluation details. " * 10)
    report = "## 关键结果\n\n- Alpha 模型准确率为 91.2。"
    parsed = ParsedPaper(page, [page])

    def chat(_system, prompt):
        candidate = json.loads(prompt.split("候选原文 JSON：\n", 1)[1])[0]
        return response(("- Alpha 模型准确率为 91.2", [candidate["id"]], "。"))

    assert repair_numeric_citations(report, parsed, chat) == report


def test_rewrites_thousands_count_to_exact_pdf_notation():
    page = ("Annotation workforce includes 2 vendors and 20 annotators. "
            "Annotation cost Approx. $45k total; $6.5/SVG.")
    digest = hashlib.sha256(json.dumps([page], ensure_ascii=False).encode()).hexdigest()[:24]
    old = f"Q1-{digest}-1-0-{page.index('Annotation cost')}"
    report = ("## 可复现性\n\n- 标注团队有 2 vendors "
              f"[[证据ID:{old}]]；成本约 45,000 美元，平均 $6.5/SVG。")
    parsed = ParsedPaper(page, [page])

    def chat(_system, prompt):
        candidate = json.loads(prompt.split("候选原文 JSON：\n", 1)[1])[0]
        return response(
            ("- 标注团队有 2 vendors", [old], "；"),
            ("成本约 $45k，平均 $6.5/SVG", [candidate["id"]], "。"),
        )

    repaired = repair_numeric_citations(report, parsed, chat)
    output, evidence = attach_evidence(repaired, parsed, pdf_available=True, full_report=True)
    assert "$45k" in output
    assert not evidence["numeric_audit"]["issues"]


def test_report_generation_runs_repair_before_publication(monkeypatch):
    page = ("SVGAnim-SFT contains 123k training samples. "
            "The remaining 1k samples form a held-out test set.")
    parsed = ParsedPaper(page, [page])
    paper = Paper.from_dict({"arxiv_id": "2605.01517", "title": "Paper", "abstract": page})
    generator = ReportGenerator(type("LLM", (), {"enabled": True})(), LLMConfig())
    calls = []

    def fake_chat(_self, key, _system, prompt):
        calls.append(key)
        if key.startswith("chunk-"):
            return "数据集分为监督训练和独立测试。"
        if key == "report":
            return "# Paper\n\n## 实验设置\n\n- SVGAnim-SFT 含 123k 样本；剩余 1k 样本作为测试集。"
        candidates = json.loads(prompt.split("候选原文 JSON：\n", 1)[1])
        source = next(item for item in candidates if "remaining 1k" in item["text"])
        return response(
            ("- SVGAnim-SFT 含 123k 样本", [source["id"]], "；"),
            ("剩余 1k 样本作为测试集", [source["id"]], "。"),
        )

    monkeypatch.setattr(ReportGenerator, "_chat", fake_chat)
    result = generator.generate(paper, VerifiedMetadata(), parsed, None)
    published, evidence = attach_evidence(result, parsed, pdf_available=True, full_report=True)
    assert "numeric-citation-repair-v1-1" in calls
    assert "1k 样本作为测试集" in published
    assert not evidence["numeric_audit"]["issues"]


def test_clean_report_needs_no_extra_model_request():
    page = "Alpha model accuracy is 91.2 on the test set."
    parsed = ParsedPaper(page, [page])
    report = f"## 关键结果\n\n- Alpha model accuracy 为 91.2 [[证据:{page}]]。"

    def chat(_system, _prompt):
        raise AssertionError("no repair request should be made")

    assert repair_numeric_citations(report, parsed, chat) == report


def test_all_prose_issues_are_processed_in_bounded_batches():
    values = [f"{91 + index}.5" for index in range(9)]
    page = " ".join(f"Alpha accuracy is {value} in experiment {index + 1}."
                    for index, value in enumerate(values))
    report = "## 关键结果\n\n" + "\n".join(
        f"- Alpha accuracy 为 {value}。" for value in values
    )
    parsed = ParsedPaper(page, [page])
    calls = []

    def chat(_system, prompt):
        calls.append(prompt)
        issues = json.loads(prompt.split("待核对原句 JSON：\n", 1)[1].split("\n候选原文 JSON：\n", 1)[0])
        candidates = json.loads(prompt.split("候选原文 JSON：\n", 1)[1])
        repairs = []
        for issue in issues:
            value = next(value for value in values if value in issue["original"])
            source = next(item for item in candidates if value in item["text"])
            repairs.append({"index": issue["index"], "segments": [{
                "text": issue["original"], "source_ids": [source["id"]], "separator": "",
            }]})
        return json.dumps({"repairs": repairs})

    repaired = repair_numeric_citations(report, parsed, chat)
    _, evidence = attach_evidence(repaired, parsed, pdf_available=True, full_report=True)
    assert len(calls) == 2
    assert not evidence["numeric_audit"]["issues"]
