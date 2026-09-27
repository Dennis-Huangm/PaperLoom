"""Regressions motivated by the local, real-paper audit (no live requests)."""
from types import SimpleNamespace

from arxiv_ra.comparison import ComparisonService, select_citations
from arxiv_ra.config import LLMConfig
from arxiv_ra.evidence import attach_evidence
from arxiv_ra.models import Paper, ParsedPaper, VerifiedMetadata
from arxiv_ra.report import ReportGenerator, source_supplement


def test_primary_front_matter_and_wrapped_resource_survive_lossy_chunk_notes():
    # VectorEdits v1 p1: the historic report said the dataset URL was absent.
    front = ("VECTOREDITS\nMasaryk University\n"
             "Our dataset can be found on https://huggingface.co/\n"
             "datasets/mikronai/VectorEdits\n")
    prompts = []
    def chat(system, user):
        prompts.append((system, user))
        return "本片段未出现资源链接。" if len(prompts) == 1 else "# Paper\n正文"
    paper = Paper("2506.15903", "VectorEdits", [], "abstract", ["cs.LG"], "cs.LG",
                  None, None, "https://arxiv.org/abs/2506.15903v1", "", version=1)
    parsed = ParsedPaper(front, [front], total_pages=1)
    ReportGenerator(SimpleNamespace(enabled=True, chat=chat), LLMConfig()).generate(
        paper, VerifiedMetadata(), parsed, None)
    assert len(prompts) == 2
    synthesis = prompts[-1][1]
    assert front.strip() in synthesis
    assert "- arXiv 类别：cs.LG" in synthesis
    assert "- arXiv 修订版：1" in synthesis
    # The source remains literal; no URL reachability check or guessed path.
    assert "https://huggingface.co/datasets" not in source_supplement(parsed)


def test_resource_supplement_preserves_late_appendix_and_parser_only_links():
    pages = ["front", "appendix\nCode at https://example.org/appendix\nRelease notes"]
    parsed = ParsedPaper("Parser retained https://example.org/parser-only reference", pages)
    result = source_supplement(parsed)
    assert "https://example.org/appendix" in result
    assert "https://example.org/parser-only" in result
    assert result.count("https://example.org/appendix") == 1
    assert "可能属于参考文献" in result


def test_source_supplement_is_bounded_and_does_not_read_files(tmp_path):
    secret = tmp_path / "private.txt"
    secret.write_text("never include this", encoding="utf-8")
    text = ("a" * 20000) + "\n" + "\n".join(f"https://example.org/{i}\n" + "z" * 800 for i in range(100))
    parsed = ParsedPaper(text, [str(secret) + "\n" + text])
    result = source_supplement(parsed)
    assert len(result) < 12000
    assert "never include this" not in result
    assert "https://example.org/99" not in result


def test_real_table_precision_error_is_flagged_but_correct_literal_is_supported():
    # SVGEditBench v1 Table 1 prints Original DINO=0.897, not historic 0.8971.
    quote = "Original\n0.061\n0.897\n26.882\nGround Truth\n0\n1\n27.492"
    parsed = ParsedPaper(quote, [quote], total_pages=1)
    for value, expected in [("0.8971", True), ("0.897", False)]:
        _, evidence = attach_evidence(f"# Paper\n\n## 关键结果\nOriginal DINO {value}。[[证据:{quote}]]",
                                      parsed, pdf_available=True, full_report=True)
        assert bool(evidence["numeric_audit"]["issues"]) == expected


def test_metric_scope_error_remains_explicit_manual_limit():
    # Real VectorEdits table: no-edit wins CLIP/DINO, GPT-4o mini wins MSE.
    # Literal containment cannot prove that the broad claim is entailed.
    quote = "Baseline no edit CLIP 0.9634 DINO 0.9011 MSE 10488. GPT-4o mini MSE 8526."
    _, evidence = attach_evidence("# Paper\n\n## 关键结果\n所有指标均不如基线。[[证据:" + quote + "]]",
                                  ParsedPaper(quote, [quote]), pdf_available=True, full_report=True)
    assert evidence["validated_citations"] == 1
    assert evidence["numeric_audit"]["semantic_support"] == "not_assessed"


def test_mixed_sources_cannot_launder_report_details_as_primary_claims():
    abstract = "The benchmark evaluates instruction based vector editing."
    report = "The report infers that domain diversity may be limited."
    snapshot = {"sources": [{"id": "P1", "evidence": [
        {"id": "P1:A", "kind": "abstract", "text": abstract},
        {"id": "P1:R", "kind": "report", "text": report}]}]}
    cell = {"text": "该基准研究向量编辑，但领域多样性可能受限。", "kind": "source",
            "evidence": ["P1:A", "P1:R"],
            "support": [{"evidence": "P1:A", "quote": abstract}, {"evidence": "P1:R", "quote": report}]}
    matrix, rejected = ComparisonService._matrix(snapshot, {"papers": [{"id": "P1", "dimensions": {"局限性": cell}}]})
    accepted = matrix["P1"]["局限性"]
    assert rejected == 0
    assert accepted["kind"] == "inference"
    assert accepted["basis"] == "mixed_sources"
    assert accepted["support"] == cell["support"]


def test_mixed_source_does_not_relax_primary_numeric_requirement():
    snapshot = {"sources": [{"id": "P1", "evidence": [
        {"id": "P1:A", "kind": "abstract", "text": "Editing remains challenging for current models."},
        {"id": "P1:R", "kind": "report", "text": "Original Ground Truth DINO 0.8971."}]}]}
    cell = {"text": "Original 的 DINO 为 0.8971。", "kind": "source", "evidence": ["P1:A", "P1:R"],
            "support": [{"evidence": e["id"], "quote": e["text"]} for e in snapshot["sources"][0]["evidence"]]}
    matrix, rejected = ComparisonService._matrix(snapshot, {"papers": [{"id": "P1", "dimensions": {"关键结果": cell}}]})
    assert rejected == 1
    assert matrix["P1"]["关键结果"]["rejection_reason"] == "numeric_mismatch"


def test_short_late_result_rows_survive_comparison_quote_selection():
    # Real SVGEditBench regeneration had 50 quotes / 5590 chars; GPT-4o mini
    # was quote 39 and would disappear under the former first-20 policy.
    prose = [{"page": 1, "quote": "Source prose " * 15, "sections": ["核心方法"]} for _ in range(20)]
    rows = [{"page": 5, "quote": f"Model {i} MSE 0.070 DINO 0.875", "sections": ["关键结果"]} for i in range(24)]
    limits = [{"page": 7, "quote": "Training an editing model was not our primary focus.", "sections": ["局限性"]}]
    citations = prose + rows + limits
    assert select_citations(citations) == citations


def test_comparison_quote_budget_keeps_late_sections_and_source_order():
    early = [{"page": 1, "quote": "a" * 600, "sections": ["核心方法"]} for _ in range(80)]
    result = {"page": 10, "quote": "Result " * 80, "sections": ["关键结果"]}
    limitation = {"page": 11, "quote": "Limit " * 80, "sections": ["局限性"]}
    selected = select_citations(early + [result, limitation, None, {"page": 0, "quote": "invalid"}])
    assert result in selected and limitation in selected
    assert selected[-2:] == [result, limitation]
    assert len(selected) <= 60 and sum(len(c["quote"][:1000]) for c in selected) <= 12000
    # Separate cap for a large number of short valid rows.
    assert len(select_citations([dict(result, quote="small row evidence") for _ in range(100)])) == 60
