from __future__ import annotations

import re

from .config import LLMConfig
from .llm import LLMClient
from .models import FigureCandidate, Paper, ParsedPaper, VerifiedMetadata
from .utils import normalize_space


CHUNK_SYSTEM = """你是严谨的 AI 论文阅读助手。仅依据提供的论文片段抽取信息，不得补写不存在的结论。
保留数据集、指标、模型、公式和数值的原名；指出信息所在页码标记。输出简洁中文。
如果片段包含关键公式，必须同时提取公式、符号定义、它连接的输入输出以及作者给出的设计目的。"""


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

    def generate(
        self,
        paper: Paper,
        metadata: VerifiedMetadata,
        parsed: ParsedPaper | None,
        main_figure: FigureCandidate | list[FigureCandidate] | None,
    ) -> str:
        if not self.llm.enabled or not parsed:
            return self._extractive_report(paper, metadata, main_figure)
        method_figures = (
            main_figure if isinstance(main_figure, list) else ([main_figure] if main_figure else [])
        )
        self._explain_figures(paper, method_figures)
        chunks = self._chunks(parsed.text)
        evidence_notes: list[str] = []
        for index, chunk in enumerate(chunks, start=1):
            note = self.llm.chat(
                CHUNK_SYSTEM,
                f"""这是论文第 {index}/{len(chunks)} 个片段：

{chunk}

请提取：研究问题、方法机制、关键公式及符号定义、主要贡献、实验设置、关键结果与数值、作者明确陈述的局限性。
没有出现的项目写“本片段未出现”，不要把 future work 自动当成局限性。""",
            )
            evidence_notes.append(note)
        metadata_text = self._metadata_text(paper, metadata)
        evidence_text = "\n\n".join(f"### 片段 {i + 1}\n{note}" for i, note in enumerate(evidence_notes))
        figure_text = (
            "实验章节之前的可靠方法图：\n"
            + "\n".join(
                f"- {figure.caption or '图注未提取到'}；AI 图示解读：{figure.explanation or '未生成'}"
                for figure in method_figures
            )
            if method_figures
            else "没有可靠方法图；不得生成任何图示相关标题、说明或占位文本。"
        )
        report = self.llm.chat(
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

必须按以下顺序输出：
# 原始英文标题
基本信息表（中文标题、作者及机构、arXiv类别、首次公开/修订/正式发表日期、会议或期刊、状态、DOI、链接、元数据来源）
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
3. 关键实验数值尽量保留页码标记；不要虚构页码。
4. 不得将 arXiv 首发日期写成正式发表日期。
5. 所有数学公式必须使用标准 LaTeX：行内公式用 `\\(...\\)`，独立公式用 `\\[...\\]`；不得用普通方括号代替公式定界符。
6. 图示内容由后续定稿器统一插入。你不得输出“方法图解析”“主图说明”或任何以 Figure/Fig. 编号开头的小节，也不得逐图复述图注；只需在普通方法叙述中准确说明机制。
7. 如果没有可靠方法图，完全跳过图示内容，不得写“未提取到主图”等提示。
8. 不得生成“阅读建议”或“核验备注”小节。
9. {CORE_METHOD_GUIDANCE}
""",
        )
        return self._normalize_title(report, paper.title)

    def _explain_figures(self, paper: Paper, figures: list[FigureCandidate]) -> None:
        for figure in figures:
            if figure.explanation:
                continue
            try:
                figure.explanation = self.llm.describe_figure(
                    figure.path,
                    paper.title,
                    paper.abstract,
                    figure.caption,
                )
            except Exception:
                try:
                    figure.explanation = self.llm.chat(
                        "你是论文图示讲解助手。不得照抄或逐字翻译原始 caption。",
                        f"""论文：{paper.title}
摘要：{paper.abstract[:1200]}
原始 caption：{figure.caption}

请用 2-4 句通俗中文重新解释图的输入、关键步骤、输出和核心含义。不要使用“图注写道”等措辞。""",
                    ).strip()
                except Exception:
                    figure.explanation = ""

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

    def _metadata_text(self, paper: Paper, metadata: VerifiedMetadata) -> str:
        authors = "; ".join(
            f"{author.name} ({', '.join(author.affiliations) if author.affiliations else '机构未核实'})"
            for author in metadata.authors
        )
        return f"""- 标题：{metadata.title or paper.title}
- 作者与机构：{authors}
- arXiv ID：{paper.arxiv_id}
- arXiv 首发：{paper.published.date().isoformat()}
- arXiv 修订：{paper.updated.date().isoformat()}
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
        authors = "、".join(author.name for author in metadata.authors)
        affiliations = sorted({aff for author in metadata.authors for aff in author.affiliations})
        return f"""# {paper.title}

> 当前未配置 LLM 或未取得全文；以下是可核验的元数据与摘要级报告，不代表完整深度阅读。

| 字段 | 内容 |
|---|---|
| 作者 | {authors} |
| 作者机构 | {'；'.join(affiliations) if affiliations else '未核实'} |
| arXiv 类别 | {', '.join(paper.categories)} |
| arXiv 首次公开 | {paper.published.date().isoformat()} |
| arXiv 最近修订 | {paper.updated.date().isoformat()} |
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
