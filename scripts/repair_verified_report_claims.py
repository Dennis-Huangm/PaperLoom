"""Rebuild two September 2026 reports after checking their claims against the PDFs.

The original model text and current published reports must still match the
stored checkpoint replay. This script intentionally targets only these reports.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

import pymupdf

from arxiv_ra.evidence import attach_evidence, restore_table_row_citations
from arxiv_ra.models import Author, FigureCandidate, Paper, ParsedPaper, VerifiedMetadata
from arxiv_ra.render import render_report
from arxiv_ra.report import finalize_report_structure
from arxiv_ra.report_metadata import protect_metadata
from arxiv_ra.source_spans import source_spans
from arxiv_ra.utils import write_json


ROOT = Path(__file__).resolve().parents[1]
REPORTS = ROOT / "run/2026-09-29/reports"
JOBS = ROOT / "run/.jobs/report-work"
TARGETS = (
    ("2603.29852", "6f3ee648e5", "20f85d0c46ba48dbac9472bb93bc28de"),
    ("2605.01517", "ec84ac29fd", "161a235483ab4cdda2e8164eb2d4b56b"),
)


def replace_once(text: str, old: str, new: str) -> str:
    count = text.count(old)
    if count != 1:
        raise ValueError(f"Expected one occurrence, found {count}: {old[:90]}")
    return text.replace(old, new, 1)


def repair_claims(report: str, issues: list[dict], parsed: ParsedPaper, arxiv_id: str) -> str:
    digest = hashlib.sha256(json.dumps(parsed.page_texts, ensure_ascii=False).encode("utf-8")).hexdigest()[:24]

    def cite(page: int, first: str, last: str) -> str:
        text = parsed.page_texts[page - 1]
        start = text.index(first)
        end = text.index(last, start) + len(last)
        quote = text[start:end]
        if not 20 <= len(re.sub(r"\s+", "", quote)) <= 600:
            raise ValueError(f"Invalid citation length on PDF page {page}: {len(quote)}")
        return f"[[证据ID:Q1-{digest}-{page}-{start}-{end}]]"

    if arxiv_id == "2603.29852":
        sampling = cite(12, "From the curated set of 7,000 samples", "used to design our VLM as a judge metric (see")
        score = cite(7, "However, our proposed Qwen3VL 8B Gym", "base Qwen3VL 8B Instruct (33.00)")
        cost = cite(11, "Annotation workforce", "Approx. $45k total; $6.5/SVG")
        replacements = {
            0: f"- **数据源与规模**：论文称候选池约 2.3M 个 SVG，经筛选标注 6.9k 个 [[证据ID:S1-{digest}-11-2299-2791]] [[证据ID:S1-{digest}-26-363-817]]；每项生成任务使用 6.5k 个样本 [[证据ID:S1-{digest}-23-0-477]]。测试集保留 SVG-Stack 测试划分中的 300 个样本，验证集从训练划分选取 100 个样本 {sampling}。",
            4: f"*Sketch2SVG 正文报告：Qwen3VL 8B Gym 的 Task Score 为 70.72，高于 GPT-4o 的 69.55 和 Qwen3VL 235B 的 67.52 {score}；VLM Judge 为 46.00，高于基础 Qwen3VL 8B Instruct 的 33.00 {score}。SVG Editing 的 Task Score 为 82.81，MSE 为 8.36，基础 8B 的 MSE 为 11.01 [[证据ID:S1-{digest}-7-2683-3169]]。*",
            5: f"- **标注团队与质控**：论文列出 2 家标注供应商、20 多名标注人员 {cost}；成本约 $45k，总体平均 $6.5/SVG {cost}。样本通过语法、渲染检查与专家复核。",
        }
    else:
        split_sft = cite(5, "SVGAnim-SFT subset contains 123k", "sparse state updates.")
        split_rl = cite(5, "The SVGAnim-RL subset consists of 10k", "path-level deformations.")
        split_test = cite(5, "The remaining 1k samples", "held-out test set for final evaluation.")
        sft_a = cite(13, "Training Configuration. We adopt", "performed in bf16 precision for 2 epochs.")
        sft_b = cite(13, "We employ DeepSpeed ZeRO-3", "held-out validation split.")
        rl_a = cite(14, "Training Setup. GRPO training", "accumulate gradients over 16 steps.")
        rl_b = cite(14, "For each prompt, we sample G = 8", "18k tokens respectively.")
        rl_c = cite(14, "We disable column pruning", "bf16 precision throughout training.")
        frames = cite(14, "SVG-to-Video Rendering. We employ", "with up to 24 frames.")
        baseline = cite(6, "For GPT-5.2 and Gemini 3 Pro", "1,000 SDS steps")
        compression = cite(4, "Representing a 24-frame animation", "achieving a 9.86× compression ratio.")
        replacements = {
            0: f"- **数据划分**：SVGAnim-SFT 含 123k 个监督微调样本 {split_sft}；SVGAnim-RL 含 10k 个高复杂度样本 {split_rl}；剩余 1k 个样本构成独立测试集 {split_test}。",
            1: f"- **阶段 I（SFT）**：全参数微调启用 FlashAttention-2 {sft_a}；最大序列长度 25k tokens，micro-batch 为 1，梯度累积 32 步 {sft_a}；AdamW 学习率为 \\(1 \\times 10^{{-4}}\\)、权重衰减为 0，余弦调度带 5% warmup {sft_a}；bf16 训练 2 个 epochs {sft_a}。使用 DeepSpeed ZeRO-3 在 8 块 GPU 上分片，每 500 步在验证集评估 {sft_b}。",
            2: f"- **阶段 II（GRPO RL）**：在 SVGAnim-RL 上训练 2 个 epochs，batch size 为 1，梯度累积 16 步 {rl_a}。每次更新采样 \\(G=8\\) 个候选 {rl_b}；AdamW 学习率为 \\(2 \\times 10^{{-6}}\\)、betas 为 \\((0.9,0.99)\\)，KL 系数为 0.01、权重衰减为 0.1 {rl_b}；余弦调度带 10% warmup，prompt/completion 最大长度分别为 12k/18k tokens {rl_b}。关闭 column pruning 并采用 bf16 {rl_c}。",
            3: f"- **渲染环境**：使用无头浏览器渲染，跨 rollout 复用浏览器实例；每段视频最多 24 帧 {frames}。",
            4: f"- **对比基线**：GPT-5.2 与 Gemini 3 Pro 同时接收初始 SVG 代码和渲染图像 {baseline}；LiveSketch 采用官方实现，优化预算提高到 1,000 个 SDS 步 {baseline}。",
            5: f"- 使用稀疏状态更新后，论文报告 24 帧动画的表示平均为 9.2k tokens，相对完整 SVG 代码达到 9.86 倍压缩 {compression}。",
            6: f"- **训练超参数可复现性**：附录给出阶段 I 的最大序列长度 25k、FlashAttention-2 和 DeepSpeed ZeRO-3 {sft_a} {sft_b}；阶段 II 的 \\(G=8\\)、KL 系数 0.01、权重衰减 0.1 和学习率 \\(2 \\times 10^{{-6}}\\) {rl_b}。",
        }
    for index, replacement in replacements.items():
        report = replace_once(report, issues[index]["original"], replacement)
    return report


def rebuild(arxiv_id: str, suffix: str, job_id: str) -> tuple[Path, int]:
    report_dir = next(path for path in REPORTS.glob(arxiv_id + "*") if path.name.endswith(suffix))
    metadata_path = report_dir / "metadata.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    current = (report_dir / "report.md").read_text(encoding="utf-8")
    current_evidence = json.loads((report_dir / "evidence.json").read_text(encoding="utf-8"))
    if not current_evidence["numeric_audit"]["issues"]:
        return report_dir, 0
    raw = json.loads((JOBS / job_id / "report.json").read_text(encoding="utf-8"))["text"]
    paper = Paper.from_dict(metadata["paper"])
    verified_data = dict(metadata["verified"])
    verified_data["authors"] = [Author(**item) for item in verified_data["authors"]]
    verified = VerifiedMetadata(**verified_data)
    figures = [FigureCandidate(path=report_dir / item["filename"], page=item["page"],
                               caption=item["caption"], kind=item["source"],
                               explanation=item["explanation"])
               for item in json.loads((report_dir / "method-figures.json").read_text(encoding="utf-8"))]
    with pymupdf.open(report_dir / "paper.pdf") as document:
        pages = [page.get_text("text") for page in document]
    parsed = ParsedPaper("", pages, total_pages=len(pages))

    def publish_text(value: str) -> tuple[str, dict]:
        value = protect_metadata(value, paper, verified)
        value = finalize_report_structure(value, figures)
        return attach_evidence(value, parsed, pdf_available=True, full_report=True)

    baseline, _ = publish_text(raw)
    if baseline != current:
        raise ValueError(f"Published {arxiv_id} report differs from its checkpoint replay")

    repaired = repair_claims(raw, current_evidence["numeric_audit"]["issues"], parsed, arxiv_id)
    if arxiv_id == "2603.29852":
        notes = [json.loads(path.read_text(encoding="utf-8"))["text"]
                 for path in sorted((JOBS / job_id).glob("chunk-*.json"))]
        repaired = restore_table_row_citations(repaired, notes, parsed, source_spans(parsed))
    result, evidence = publish_text(repaired)
    remaining = evidence["numeric_audit"]["issues"]
    if remaining or evidence["rejected_citations"]:
        details = [(item["section"], item["numbers"], item["reason"], item["original"][:160])
                   for item in remaining]
        raise ValueError(f"{arxiv_id}: {len(remaining)} numeric issues, {evidence['rejected_citations']} rejected citations: {details}")

    (report_dir / "report.md").write_text(result, encoding="utf-8")
    write_json(report_dir / "evidence.json", evidence)
    metadata["evidence"] = {key: value for key, value in evidence.items() if key != "citations"}
    write_json(metadata_path, metadata)
    render_report(result, report_dir / "report.html", paper.title, arxiv_id=paper.arxiv_id,
                  profile_id=metadata["profile_id"],
                  report_id=(report_dir / "report.html").relative_to(ROOT / "run").as_posix())
    return report_dir, len(current_evidence["numeric_audit"]["issues"])


if __name__ == "__main__":
    for target in TARGETS:
        path, count = rebuild(*target)
        print(f"{path}: checked and repaired {count} original warnings")
