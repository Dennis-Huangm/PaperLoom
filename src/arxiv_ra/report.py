from __future__ import annotations

import re

from .config import LLMConfig
from .llm import LLMClient
from .models import FigureCandidate, Paper, ParsedPaper, VerifiedMetadata
from .utils import normalize_space
from .task_runtime import task_warning, task_progress, task_checkpoint
from .report_checkpoint import CheckpointWriteError, current_report_checkpoint, file_digest
from .evidence import EVIDENCE_GUIDANCE, restore_table_row_citations, source_table_inventory
from .citation_repair import repair_numeric_citations
from .quality import QUALITY_GUIDANCE
from .source_spans import (source_spans, span_batches, span_material, cited_span_material,
                           ground_note_quotes, ID_GUIDANCE, SYNTHESIS_ID_GUIDANCE)
from .report_metadata import protect_metadata
from .report_completeness import restore_note_tables


CHUNK_SYSTEM = """你是严谨的 AI 论文阅读助手。仅依据提供的论文片段抽取信息，不得补写不存在的结论。
保留数据集、指标、模型、公式和数值的原名；保留连续原文摘录以便后续定位。输出简洁中文。
如果片段包含关键公式（包括附录中的指标定义），必须同时提取公式、符号定义、它连接的输入输出以及作者给出的设计目的。
保留本片段出现的作者机构、代码/数据集/项目链接和复现参数，不要因其位于脚注或附录而省略。
逐表保留实验表格，使用含原文 Table 编号的标题和 Markdown 表格，保留各模型行、表头、条件、单位与脚注；不要只摘最优结果。跨片段表格标注摘录，不猜测缺失部分。
片段编号是内部处理顺序，不是 PDF 页码，也不是可引用的原文位置。"""


def source_supplement(parsed: ParsedPaper) -> str:
    """Pass bounded original front matter and URL contexts past lossy chunk notes.

    Keep raw line breaks (including wrapped URLs); this is source material, not
    a resolved link inventory or a claim that a URL is reachable or relevant.
    """
    front = parsed.page_texts[0] if parsed.page_texts else parsed.text
    front = front[:5000]
    blocks = ["首页原文节选（可能截断；作者自述不等于外部核验）：\n" + front]
    # Also examine parser Markdown, which may retain URLs lost by PDF extraction.
    texts = [*parsed.page_texts, parsed.text]
    seen = set()
    for text in texts:
        for match in re.finditer(r"https?://[^\s<>]+", text):
            start = max(0, match.start() - 100)
            end = min(len(text), match.end() + 220)
            excerpt = text[start:end].strip()
            key = normalize_space(excerpt)
            if not key or key in seen or excerpt in front:
                continue
            seen.add(key)
            blocks.append("链接附近原文（可能属于参考文献，需判断归属）：\n" + excerpt[:500])
            if len(seen) == 12:
                return "\n\n".join(blocks)
    return "\n\n".join(blocks)


CORE_METHOD_GUIDANCE = """“核心方法”必须比摘要更深入，并独立于“方法图解析”完成以下说明：
- 先给出方法总览，说明输入、输出、整体数据流以及训练与推理阶段的区别；
- 再按实际依赖关系解释关键模块、信息如何传递、每个模块解决什么问题，避免只罗列模块名称；
- 如果证据中存在关键公式，逐个保留并解析：定义所有主要符号，解释计算顺序与输入输出，说明设计动机、优化目标，以及它如何影响训练信号、推理决策或最终结果；
- 公式解析必须紧跟对应公式，不得只翻译公式前后的原文，也不得根据常识虚构论文没有给出的公式、符号含义或因果结论；
- 可使用“方法总览”“关键模块与流程”“公式与目标函数解析”等三级标题；若论文没有关键公式，则自然省略公式小节，不得生成占位说明。"""


REPORT_SECTION_ORDER = (
    "基本信息表",
    "一句话总结",
    "为什么值得阅读",
    "研究问题与背景",
    "核心方法",
    "主要贡献",
    "实验设置",
    "关键结果",
    "与已有工作的区别",
    "局限性",
    "可复现性",
)


def _pop_section(markdown_text: str, heading: str) -> tuple[str, str]:
    pattern = re.compile(
        rf"(?ms)^##\s+{re.escape(heading)}\s*\n(.*?)(?=^##\s+|\Z)"
    )
    match = pattern.search(markdown_text)
    if not match:
        return "", markdown_text
    content = match.group(1).strip()
    return content, markdown_text[: match.start()] + markdown_text[match.end() :]


def _pop_subsection(markdown_text: str, heading: str) -> tuple[str, str]:
    pattern = re.compile(
        rf"(?ms)^###\s+{re.escape(heading)}\s*\n(.*?)(?=^(?:##|###)\s+|\Z)"
    )
    match = pattern.search(markdown_text)
    if not match:
        return "", markdown_text
    content = match.group(1).strip()
    return content, markdown_text[: match.start()] + markdown_text[match.end() :]


def _remove_all_subsections(markdown_text: str, heading: str) -> tuple[list[str], str]:
    """Remove every matching H3 block instead of leaving later duplicates behind."""
    removed: list[str] = []
    while True:
        content, updated = _pop_subsection(markdown_text, heading)
        if updated == markdown_text:
            return removed, markdown_text
        if content:
            removed.append(content)
        markdown_text = updated


def _normalize_top_level_sections(markdown_text: str) -> str:
    """Keep one canonical H2 sequence while retaining unknown appendices at the end."""
    title_match = re.match(r"(?s)\s*(#\s+[^\n]+)\n(.*)", markdown_text)
    if not title_match:
        return markdown_text
    title, remainder = title_match.groups()
    matches = list(re.finditer(r"(?m)^##\s+([^\n]+?)\s*$", remainder))
    if not matches:
        return markdown_text
    preamble = remainder[: matches[0].start()].strip()
    sections: list[tuple[str, str]] = []
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(remainder)
        sections.append((match.group(1).strip(), remainder[match.end() : end].strip()))

    by_heading: dict[str, list[str]] = {}
    for heading, content in sections:
        by_heading.setdefault(heading, []).append(content)
    ordered: list[str] = [title]
    if preamble:
        ordered.append(preamble)
    for heading in REPORT_SECTION_ORDER:
        contents = [item for item in by_heading.pop(heading, []) if item]
        if contents:
            # Repeated H2 headings are consolidated rather than silently discarded.
            ordered.append(f"## {heading}\n\n" + "\n\n".join(contents))
    for heading, contents in by_heading.items():
        content = "\n\n".join(item for item in contents if item)
        ordered.append(f"## {heading}" + (f"\n\n{content}" if content else ""))
    return "\n\n".join(ordered)


def _strip_generated_figure_commentary(markdown_text: str) -> str:
    """Remove model-authored Figure H3 blocks; the deterministic figure block owns them."""
    core_match = re.search(
        r"(?ms)^##\s+核心方法\s*\n(.*?)(?=^##\s+|\Z)", markdown_text
    )
    if not core_match:
        return markdown_text
    core = core_match.group(1)
    core = re.sub(
        r"(?ms)^###\s+(?:Figure|Fig\.?)\s*\d+[^\n]*\n.*?(?=^###\s+|\Z)",
        "",
        core,
    )
    core = re.sub(r"\n{3,}", "\n\n", core).strip()
    suffix = markdown_text[core_match.end(1) :].lstrip()
    return markdown_text[: core_match.start(1)] + core + "\n\n" + suffix


def _repair_inline_canonical_headings(markdown_text: str) -> str:
    """Recover a canonical H2 accidentally joined to the preceding paragraph."""
    names = "|".join(re.escape(item) for item in REPORT_SECTION_ORDER)
    return re.sub(
        rf"(?<!\n)(##\s+(?:{names})\s*)",
        lambda match: "\n\n" + match.group(1),
        markdown_text,
    )


def finalize_report_structure(
    markdown_text: str,
    main_figure: FigureCandidate | list[FigureCandidate] | None,
) -> str:
    """Apply the stable outline and place pre-experiment method figures in 核心方法."""
    from .table_quality import normalize_table_separators
    from .evidence import TOKEN
    markdown_text = normalize_table_separators(markdown_text, TOKEN)
    markdown_text = _repair_inline_canonical_headings(markdown_text)
    method_figures = (
        main_figure
        if isinstance(main_figure, list)
        else ([main_figure] if main_figure is not None else [])
    )
    figure_analysis = ""
    for heading in ("主图说明", "主图"):
        content, markdown_text = _pop_section(markdown_text, heading)
        if content and not figure_analysis:
            figure_analysis = content
    for heading in ("方法主图解析", "方法图解析"):
        contents, markdown_text = _remove_all_subsections(markdown_text, heading)
        for content in contents:
            if content and not figure_analysis:
                if heading == "方法主图解析":
                    figure_analysis = content
                else:
                    interpretation = re.search(
                        r"(?ms)^####\s+图示解读\s*\n(.*)$", content
                    )
                    if interpretation:
                        figure_analysis = interpretation.group(1).strip()
    for heading in ("阅读建议", "核验备注"):
        _unused, markdown_text = _pop_section(markdown_text, heading)

    # Older reports exposed the extraction source beneath every image. It is
    # implementation metadata rather than part of the reading explanation.
    markdown_text = re.sub(
        r"(?m)^\*\*(?:Figure|Fig\.?)\s*\d+\s*·\s*(?:arXiv HTML|PDF 第\s*\d+\s*页)\*\*\s*$",
        "",
        markdown_text,
    )

    markdown_text = re.sub(
        r"(?m)^!\[[^\]]*(?:主图|方法图|候选主图)[^\]]*\]\((?:main|method)-figure[^)]*\)\s*$",
        "",
        markdown_text,
    )
    if method_figures:
        markdown_text = _strip_generated_figure_commentary(markdown_text)
        markdown_text = markdown_text.replace("论文候选主图", "论文方法主图").replace(
            "候选主图", "方法主图"
        )
        figure_analysis = figure_analysis.replace("论文候选主图", "论文方法主图").replace(
            "候选主图", "方法主图"
        )
        clean_analysis = re.sub(
            r"(?m)^!\[[^\]]*\]\((?:main|method)-figure[^)]*\)\s*$", "", figure_analysis
        ).strip()
        blocks: list[str] = ["### 方法图解析"]
        for index, figure in enumerate(method_figures, start=1):
            number_match = re.match(r"(?:Figure|Fig\.?)\s*(\d+)", figure.caption, re.I)
            label = f"Figure {number_match.group(1)}" if number_match else f"Figure {index}"
            blocks.extend(
                [
                    f"#### {label}",
                    f"![论文方法图 {label}]({figure.path.name})",
                    "**通俗解读**",
                    figure.explanation
                    or "这张图用于概括该部分的方法结构；具体模块含义请结合紧随其后的核心方法说明阅读。",
                ]
            )
        if clean_analysis and not all(figure.explanation for figure in method_figures):
            blocks.extend(["#### 图示解读", clean_analysis])
        figure_block = "\n\n".join(part for part in blocks if part).strip()
        if re.search(r"(?m)^##\s+核心方法\s*$", markdown_text):
            markdown_text = re.sub(
                r"(?m)^(##\s+核心方法\s*)$",
                lambda match: f"{match.group(1)}\n\n{figure_block.rstrip()}",
                markdown_text,
                count=1,
            )
    markdown_text = _normalize_top_level_sections(markdown_text)
    markdown_text = re.sub(r"\n{3,}", "\n\n", markdown_text).strip() + "\n"
    return markdown_text


class ReportGenerator:
    def __init__(self, llm: LLMClient, config: LLMConfig) -> None:
        self.llm = llm
        self.config = config

    def _chat(self, key, system, user):
        checkpoint = current_report_checkpoint()
        inputs = [system, user]
        cached = checkpoint.get(key, inputs) if checkpoint else None
        if cached is not None:
            return cached
        task_checkpoint()
        result = self.llm.chat(system, user)
        if checkpoint:
            checkpoint.put(key, inputs, result)
        return result

    def generate(
        self,
        paper: Paper,
        metadata: VerifiedMetadata,
        parsed: ParsedPaper | None,
        main_figure: FigureCandidate | list[FigureCandidate] | None,
    ) -> str:
        if not self.llm.enabled:
            return self._extractive_report(paper, metadata, main_figure)
        if not parsed or not parsed.text.strip():
            return self._extractive_report(paper, metadata, main_figure)
        method_figures = (
            main_figure if isinstance(main_figure, list) else ([main_figure] if main_figure else [])
        )
        checkpoint = current_report_checkpoint()
        if checkpoint:
            checkpoint.configure_model(self.config, self.llm)
        self._explain_figures(paper, method_figures)
        chunks, banks, spans = self.analysis_inputs(parsed)
        evidence_notes: list[str] = []
        if checkpoint:
            checkpoint.publish(chunks_total=len(chunks), chunks_done=0)
        for index, chunk in enumerate(chunks, start=1):
            task_progress(f"正在分析或复用正文分片 {index}/{len(chunks)}…", 65 + round(17 * (index - 1) / len(chunks)))
            note = self._chat(
                f"chunk-{index}",
                CHUNK_SYSTEM + "\n" + EVIDENCE_GUIDANCE + "\n" + ID_GUIDANCE + "\n" + QUALITY_GUIDANCE,
                f"""这是论文第 {index}/{len(chunks)} 个片段：

{chunk}

独立 PDF 原文片段（可能与解析正文分片不同；同时提取这里的证据，引用实际 ID）：
{span_material(banks[index - 1])}

请提取：研究问题、方法机制、关键公式及符号定义、主要贡献、实验设置、关键结果与数值、作者明确陈述的局限性。
没有出现的项目写“本片段未出现”，不要把 future work 自动当成局限性。
另保留资源链接及附录中的指标定义；表格行与列归属不清楚时标为待核对，不猜测。""",
            )
            cleaned, _ = cited_span_material([note], {s["source_id"]: s for s in banks[index - 1]})
            evidence_notes.append(cleaned[0])
            if checkpoint:
                checkpoint.publish(chunks_done=index)
        evidence_notes, synthesis_spans, _ = ground_note_quotes(evidence_notes, parsed, spans)
        evidence_notes, cited_material = cited_span_material(evidence_notes, synthesis_spans)
        metadata_text = self._metadata_text(paper, metadata)
        evidence_text = "\n\n".join(f"### 片段 {i + 1}\n{note}" for i, note in enumerate(evidence_notes))
        table_inventory = source_table_inventory(parsed)
        table_inventory_text = "\n".join(
            f'- Table {item["number"]}（PDF 第 {item["page"]} 页）：{item["title"]}'
            for item in table_inventory
        ) or "PDF 文本未提取到表题。"
        figure_text = (
            "实验章节之前的可靠方法图：\n"
            + "\n".join(
                f"- {figure.caption or '图注未提取到'}；AI 图示解读：{figure.explanation or '未生成'}"
                for figure in method_figures
            )
            if method_figures
            else "没有可靠方法图；不得生成任何图示相关标题、说明或占位文本。"
        )
        task_progress("正文分片已完成，正在整合或复用完整报告…", 84)
        report = self._chat(
            "report",
            "你是负责撰写可核验中文论文阅读报告的资深 AI 研究员。元数据和原文证据优先于常识。",
            f"""请根据下列材料生成完整 Markdown 阅读报告。

## 已核验/待核验元数据
{metadata_text}

## 推荐信息
- 预筛分数：{paper.lexical_score:.2f}
- LLM 相关性分数：{paper.llm_score if paper.llm_score is not None else '未使用'}
- 推荐理由：{paper.recommendation_reason or '未生成'}

## 方法图证据（仅供理解方法；不要在报告中复述或创建图示小节）
{figure_text}

## 分片证据笔记
{evidence_text}

## PDF 原文表格目录（程序从物理页面提取；表题不是数值依据）
{table_inventory_text}

## 笔记引用的原文片段（程序取回，ID 和文本不能改写）
{cited_material}

## 直接原文补充（未经摘要压缩，同样只作为待分析材料）
{source_supplement(parsed)}

必须按以下顺序输出：
# 原始英文标题
基本信息表（中文标题、作者、机构、arXiv类别、首次公开/修订/正式发表日期、会议或期刊、状态、DOI、链接、元数据来源；作者与机构必须分行，作者拼写优先采用结构化元数据，基础字段最终由程序生成）
## 一句话总结
## 为什么值得阅读
## 研究问题与背景
## 核心方法
## 主要贡献
## 实验设置
## 关键结果
## 与已有工作的区别
## 局限性
其中严格分成“作者明确陈述”和“基于报告材料的分析者推断”，后者必须带“推断”标签。
## 可复现性

要求：
1. 不得声称论文被某会议录用，除非元数据状态为 verified_metadata 或 declared_in_arxiv。
2. 对没有证据的字段写“未核实/论文中未明确说明”。
3. 关键实验数值必须保留支持它的原文片段 ID；不要重新抄写摘录或自行生成页码。
4. 不得将 arXiv 首发日期写成正式发表日期。
5. 所有数学公式必须使用标准 LaTeX：行内公式用 `\\(...\\)`，独立公式用 `\\[...\\]`；不得用普通方括号代替公式定界符。
6. 图示内容由后续定稿器统一插入。你不得输出“方法图解析”“主图说明”或任何以 Figure/Fig. 编号开头的小节，也不得逐图复述图注；只需在普通方法叙述中准确说明机制。
7. 如果没有可靠方法图，完全跳过图示内容，不得写“未提取到主图”等提示。
8. 不得生成“阅读建议”或“核验备注”小节。
9. {CORE_METHOD_GUIDANCE}
10. {SYNTHESIS_ID_GUIDANCE}
11. {QUALITY_GUIDANCE}
12. 不得将内部片段编号当作页码或证据出处；最终输出不出现“第 N/M 片段”。保留原文章节/表号及有效 ID，由系统定位。
13. 写“未提供”前核对全部分片和直接原文补充；仅笔记未保留、公式解析不清或材料截断时，写“当前材料未能确认”，不能断言论文没有提供。
14. 原文明确给出的作者机构和资源网址应保留并注明来自论文；资源链接未经在线可用性验证。不得把参考文献网址当作本文资源，不补造或猜测网址。
15. 公式后保留对应公式的原文片段 ID；定义或相关任务描述不能替代公式自身的出处。优先用标准 Markdown 表格语法重现论文主结果表、与人类评价对齐表、关键消融和鲁棒性表，不以截图、图片链接或纯文字概括代替表格；有不同实验条件、任务或指标的表应分别呈现。保留表号、原始模型名、行列指标、轮次、单位和比较基线，不可只挑最优模型。每个数值行保留对应原文片段 ID；确实无法建立行列归属时注明待核对，不要猜测。
16. 写完后对照“PDF 原文表格目录”检查实验表格覆盖；未重现的关键实验表格说明原因。目录中的表号和标题只用于查漏，不能作为数值依据。
17. 对“随难度增加均下降”“单调退化”等趋势，逐模型、逐指标检查相邻难度；总体趋势不能写成每一行都成立。存在回升或指标间分歧时给出反例；原文作者的概括与表格观察分开表述，不能把作者概括强化成“不可避免”。
""",
        )
        if checkpoint:
            checkpoint.publish(report_ready=True)
        report = restore_note_tables(report, evidence_notes, table_inventory)
        report = restore_table_row_citations(report, evidence_notes, parsed, synthesis_spans)
        task_progress("正在回查实验数值引用…", 85)
        try:
            repair_batch = 0

            def chat_repair(system: str, user: str) -> str:
                nonlocal repair_batch
                repair_batch += 1
                return self._chat(f"numeric-citation-repair-v1-{repair_batch}", system, user)

            report = repair_numeric_citations(report, parsed, chat_repair)
        except CheckpointWriteError:
            raise
        except Exception as exc:
            task_warning("报告数值引用补核", f"自动补核未完成，未确认内容将标注待核对：{type(exc).__name__}: {exc}")
        return protect_metadata(self._normalize_title(report, paper.title), paper, metadata)

    def _explain_figures(self, paper: Paper, figures: list[FigureCandidate]) -> None:
        checkpoint = current_report_checkpoint()
        for index, figure in enumerate(figures):
            if figure.explanation:
                continue
            inputs = ["figure-explanation-v1", file_digest(figure.path), paper.title, paper.abstract, figure.caption] if checkpoint else None
            cached = checkpoint.get(f"figure-{index}", inputs) if checkpoint else None
            if cached:
                figure.explanation = cached
                continue
            task_checkpoint()
            try:
                figure.explanation = self.llm.describe_figure(
                    figure.path,
                    paper.title,
                    paper.abstract,
                    figure.caption,
                )
            except Exception as vision_exc:
                try:
                    figure.explanation = self.llm.chat(
                        "你是论文图示讲解助手。不得照抄或逐字翻译原始 caption。",
                        f"""论文：{paper.title}
摘要：{paper.abstract[:1200]}
原始 caption：{figure.caption}

请用 2-4 句通俗中文重新解释图的输入、关键步骤、输出和核心含义。不要使用“图注写道”等措辞。""",
                    ).strip()
                except Exception as text_exc:
                    task_warning(
                        "LLM 图片解读",
                        "方法图的视觉与文本解读均失败，报告将保留原始图注。"
                        f"请检查模型是否支持图片输入（{type(vision_exc).__name__}; {type(text_exc).__name__}）。",
                    )
                    figure.explanation = ""
            if checkpoint and figure.explanation:
                checkpoint.put(f"figure-{index}", inputs, figure.explanation)

    @staticmethod
    def _normalize_title(report: str, paper_title: str) -> str:
        """Replace a literal prompt placeholder with the verified paper title."""
        lines = report.splitlines()
        for index, line in enumerate(lines):
            if line.strip() == "# 原始英文标题":
                lines[index] = f"# {paper_title}"
                break
        return "\n".join(lines)

    def _chunks(self, text: str) -> list[str]:
        size = max(4000, self.config.max_chunk_chars)
        chunks: list[str] = []
        cursor = 0
        while cursor < len(text):
            end = min(cursor + size, len(text))
            if end < len(text):
                boundary = text.rfind("\n\n", cursor + size // 2, end)
                if boundary > cursor:
                    end = boundary
            chunks.append(text[cursor:end])
            cursor = end
        return chunks or [""]

    def analysis_inputs(self, parsed: ParsedPaper) -> tuple[list[str], list[list[dict]], dict]:
        """Return the exact bounded call plan, also used by live request budgets."""
        chunks = self._chunks(parsed.text)
        spans = source_spans(parsed)
        bank_size = sum(len(s["quote"]) for s in spans.values())
        chunk_size = max(4000, self.config.max_chunk_chars)
        count = max(len(chunks), (bank_size + chunk_size - 1) // chunk_size)
        chunks.extend([""] * (count - len(chunks)))
        return chunks, span_batches(spans, count), spans

    def _metadata_text(self, paper: Paper, metadata: VerifiedMetadata) -> str:
        authors = "; ".join(
            f"{author.name} ({', '.join(author.affiliations) if author.affiliations else '机构未核实'})"
            for author in (paper.authors or metadata.authors)
        )
        return f"""- 标题：{metadata.title or paper.title}
- 作者与机构：{authors}
- arXiv ID：{paper.arxiv_id}
- arXiv 修订版：{paper.version if paper.version is not None else '未核实'}
- arXiv 类别：{', '.join(paper.categories) or paper.primary_category or '未核实'}
- 发现来源：{paper.source_label}
- 摘要类型：{"完整摘要" if paper.abstract_kind == "full" else "摘要预览（待补全）"}
- arXiv 首发：{paper.published.date().isoformat() if paper.published else '未核实'}
- arXiv 修订：{paper.updated.date().isoformat() if paper.updated else '未核实'}
- 会议/期刊：{metadata.venue or '未核实'}
- 会议状态：{metadata.venue_status}
- 正式发表日期：{metadata.publication_date or '未核实'}
- DOI：{metadata.doi or '未核实'}
- 引用数：{metadata.citation_count if metadata.citation_count is not None else '未核实'}
- 元数据来源：{', '.join(metadata.sources)}
- 冲突：{'；'.join(metadata.conflicts) if metadata.conflicts else '无已知冲突'}
- arXiv：{paper.abs_url}"""

    def _extractive_report(
        self,
        paper: Paper,
        metadata: VerifiedMetadata,
        main_figure: FigureCandidate | list[FigureCandidate] | None,
    ) -> str:
        authors = "、".join(author.name for author in (paper.authors or metadata.authors))
        affiliations = sorted({aff for author in metadata.authors for aff in author.affiliations})
        return f"""# {paper.title}

> 当前未配置 LLM 或未取得全文；以下是可核验的元数据与摘要级报告，不代表完整深度阅读。

| 字段 | 内容 |
|---|---|
| 作者 | {authors} |
| 作者机构 | {'；'.join(affiliations) if affiliations else '未核实'} |
| arXiv 类别 | {', '.join(paper.categories)} |
| 发现来源 | {paper.source_label} |
| 摘要类型 | {'完整摘要' if paper.abstract_kind == 'full' else '摘要预览（待补全）'} |
| arXiv 首次公开 | {paper.published.date().isoformat() if paper.published else '未核实'} |
| arXiv 最近修订 | {paper.updated.date().isoformat() if paper.updated else '未核实'} |
| 会议或期刊 | {metadata.venue or '未核实'} |
| 状态 | {metadata.venue_status} |
| 正式发表日期 | {metadata.publication_date or '未核实'} |
| DOI | {metadata.doi or '未核实'} |
| 链接 | {paper.abs_url} |
| 元数据来源 | {', '.join(metadata.sources)} |

## 摘要级主要内容

{normalize_space(paper.abstract)}

## 推荐理由

{paper.recommendation_reason or '依据类别、关键词与发布时间完成自动预筛。'}

## 核心方法

当前为摘要级报告，论文的完整方法机制需要取得全文后进一步核实。

## 局限性

- 作者明确陈述：需要配置 LLM 并完成全文分析后提取。
- 分析者推断：当前只有摘要信息，不适合据此判断完整方法和实验局限。
"""
