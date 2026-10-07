from __future__ import annotations

import re

from .config import LLMConfig
from .llm import LLMClient
from .models import FigureCandidate, Paper, ParsedPaper, VerifiedMetadata
from .utils import normalize_space
from .task_runtime import task_warning, task_progress, task_checkpoint
from .report_checkpoint import CheckpointWriteError, current_report_checkpoint, file_digest
from .evidence import EVIDENCE_GUIDANCE, source_table_inventory
from .report_citations import strip_tokens
from .quality import QUALITY_GUIDANCE
from .source_spans import (source_spans, span_batches, span_material, cited_span_material,
                           ground_note_quotes, normalize_grouped_source_ids, ID_GUIDANCE, SYNTHESIS_ID_GUIDANCE)
from .report_metadata import protect_metadata
from .report_completeness import normalize_table_titles
from .report_tables import ReportTables, GeneratedReport


CHUNK_SYSTEM = """你是严谨的 AI 论文阅读助手。仅依据提供的论文片段抽取信息，不得补写不存在的结论。
保留数据集、指标、模型、公式和数值的原名；保留连续原文摘录以便后续定位。输出简洁中文。
如果片段包含关键公式（包括附录中的指标定义），必须同时提取公式、符号定义、它连接的输入输出以及作者给出的设计目的。
保留本片段出现的作者机构、代码/数据集/项目链接和复现参数，不要因其位于脚注或附录而省略。
逐表保留实验表格，使用含原文 Table 编号的标题和 Markdown 表格，保留各模型行、表头、条件、单位与脚注；不要只摘最优结果。逐字保留原表列名和模型行标签，包括名称的缩写，不扩展版本后缀；中文解释写在表格外。跨片段表格不猜测缺失部分。
原文表格与正文数值不一致时，矩阵内逐字保留原表数值，在表注单独说明差异；不得用正文、常识或其他版本的值“纠正”原表，不能添加原表没有的星号。原表空白单元格保持为空；若题注规定空白沿用基线，则在表注完整保留该规则，不写成“未提供”、不填充推测值。原表未命名的行标签列保留空表头，不另造“变体行”或“条件/变体”等标题。
数学表头及数值必须保留上下标和数量级，用 LaTeX 或 Unicode 上下标表达。例如参数量单位 ×10⁶ 不能平铺成 ×106，FLOPs 的 10¹⁸ 不能写成 1018；对照解析正文中的数学标记与原文上下文，不能因 PDF 文本平铺而丢失幂次。
表格说明与脚注用自然中文解释具体含义，明确规模、单位、指标方向和比较条件；保留必要英文列名，不能只贴英文表注或生硬逐词翻译。
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
    markdown_text = normalize_table_titles(markdown_text)
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
            table_pages = self._table_pages(parsed, chunk)
            table_material = "\n\n".join(
                f"[PDF 物理第 {page} 页，完整页面文本]\n{parsed.page_texts[page - 1]}"
                for page in table_pages
            ) or "本分片没有可按原表题对应的独立 PDF 表格页面。"
            note = self._chat(
                f"chunk-{index}",
                CHUNK_SYSTEM + "\n" + EVIDENCE_GUIDANCE + "\n" + ID_GUIDANCE + "\n" + QUALITY_GUIDANCE,
                f"""这是论文第 {index}/{len(chunks)} 个片段：

{chunk}

独立 PDF 原文片段（可能与解析正文分片不同；同时提取这里的证据，引用实际 ID）：
{span_material(banks[index - 1])}

本分片表格对应的完整 PDF 物理页（独立于 Docling 的列划分）：
{table_material}

请提取：研究问题、方法机制、关键公式及符号定义、主要贡献、实验设置、关键结果与数值、作者明确陈述的局限性。
没有出现的项目写“本片段未出现”，不要把 future work 自动当成局限性。
另保留资源链接及附录中的指标定义；表格行与列归属不清楚时标为待核对，不猜测。
解析正文的表头、列数或行列归属与独立 PDF 页面不同，应回查完整页面及其证据 ID，不能把解析器多出的列、重复单元格或分片边界当作原文缺失。
同一原表的全部模型、难度和指标应保留，不能把完整数值行压缩成无列归属的数值串。""",
            )
            evidence_notes.append(note)
            if checkpoint:
                checkpoint.publish(chunks_done=index)
        try:
            evidence_notes, synthesis_spans, _ = ground_note_quotes(evidence_notes, parsed, spans)
            evidence_notes, cited_material = cited_span_material(evidence_notes, synthesis_spans)
        except Exception:
            evidence_notes = [strip_tokens(note) for note in evidence_notes]
            cited_material = ''
        metadata_text = self._metadata_text(paper, metadata)
        try:
            table_inventory = source_table_inventory(parsed)
            # Import extracted cells without an independent source verdict.
            table_catalogue = ReportTables.from_notes(evidence_notes, table_inventory)
            synthesis_notes = table_catalogue.synthesis_notes(evidence_notes)
        except Exception:
            table_inventory, table_catalogue, synthesis_notes = [], ReportTables(()), evidence_notes
        evidence_text = "\n\n".join(f"### 片段 {i + 1}\n{note}" for i, note in enumerate(synthesis_notes))
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

## 已冻结的原文表格数据（只用于分析；展示由程序完成）
{table_catalogue.prompt_material()}

每张表的 id 唯一。正文在讨论对应实验的小节中，用独立一行的 `[[表格:table-N]]` 选择展示位置。
主结果可写 `[[表格:table-N|展开]]`；较大的补充矩阵可写 `[[表格:table-N|折叠]]`。
N 必须来自上述数据里的实际 id。每个 id 只放一次；其他段落用普通文字引用原表号。
{('不要输出已冻结实验表格的 Markdown 行、表头、Table 标题或表注；标题和指标定义由表格展示统一提供。' if table_catalogue.tables else '当前无托管表格，请直接根据分片笔记保留实验 Markdown 表格、中文标题、条件与脚注；不要使用表格位置指令。')}
正文负责解释结果及其意义，不在表格位置指令前后复述表题、统计口径或单位。不重新抄写矩阵，不输出数据缺失占位行。
正文复述数量时保留原始科学计数法及单位（如 FLOPs 的 ×10^9、参数量 M/B），不要将英文 billion 直接写成“亿”。如需换算，必须核验倍率：1 billion = 10^9 = 10 亿，不能只保留尾数。笔记与冻结表格的数量级不一致时回查原文，不能沿用笔记的换算。
程序会在该位置插入完整已提取表格及原有条件。仅引用未能定位时不影响数值保留。

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
15. 公式后保留对应公式的原文片段 ID；定义或相关任务描述不能替代公式自身的出处。原文主结果、与人类评价对齐、关键消融和鲁棒性表由程序从冻结数据展示。你只放置唯一表格 id，不重新创建矩阵；保留对实验条件、任务、指标、轮次、单位和比较基线的解释，不可只分析最优模型。
16. 按研究内容组织实验讨论。原文表号用于读者查阅；不要增加表格覆盖率、核查结果或处理失败的说明。
17. 对“随难度增加均下降”“单调退化”等趋势，逐模型、逐指标检查相邻难度；总体趋势不能写成每一行都成立。存在回升或指标间分歧时给出反例；原文作者的概括与表格观察分开表述，不能把作者概括强化成“不可避免”。
18. 表格放在讨论该实验、比较或成本的正文小节，紧邻解释；同一表不要分别列节选与原文摘录。保留原始表头和模型行标签，不添加“vs.”或“(本文)”等改写；说明放在表格外。不要另设“原文表格摘录”章节。
19. 表格标题使用原表号和内容名称，不添加“部分摘录”“部分数据重现”“具备文本引用之部分”等处理阶段标签。引用是否可定位不决定已提取数值是否应保留；不要用“当前材料截断”替换分片笔记中已有的单元格。确实发现缺失行列时，在对应讨论中说明具体缺失；未完成完整性检查不等于已确认缺失。
""",
        )
        if checkpoint:
            checkpoint.publish(report_ready=True)
        raw_draft = report
        # Compatibility with a model that ignores the slot instruction is a
        # projection to identities, never a second source of table values.
        try:
            report = table_catalogue.render(report)
        except Exception:
            report = table_catalogue.fallback_report(raw_draft)
        report = normalize_grouped_source_ids(report)
        report = protect_metadata(self._normalize_title(report, paper.title), paper, metadata)
        return GeneratedReport(report, table_catalogue, raw_draft)

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
        # The configured size is a prose target. A source table and its caption
        # are an atomic unit, even when larger: a bare continuation loses the
        # schema and can turn available cells into extraction placeholders.
        size = max(4000, self.config.max_chunk_chars)
        table_ranges = []
        for match in re.finditer(r"(?m)^\|[^\n]*(?:\n\|[^\n]*)+\n?", text):
            start = match.start()
            prefix = text[:start].rstrip()
            paragraph_start = prefix.rfind("\n\n") + 2
            if re.match(r"(?:#{1,6}\s*)?(?:Table\s+\d+|表\s*\d+)\s*[:：.]", prefix[paragraph_start:], re.I):
                start = paragraph_start
            table_ranges.append((start, match.end()))
        chunks: list[str] = []
        cursor = 0
        while cursor < len(text):
            end = min(cursor + size, len(text))
            if end < len(text):
                boundary = text.rfind("\n\n", cursor + size // 2, end)
                if boundary > cursor:
                    end = boundary
            for start, finish in table_ranges:
                if start <= end < finish and end > start:
                    end = start if start > cursor else finish
                    break
            chunks.append(text[cursor:end])
            cursor = end
        return chunks or [""]

    def analysis_inputs(self, parsed: ParsedPaper) -> tuple[list[str], list[list[dict]], dict]:
        """Return the exact bounded call plan, also used by live request budgets."""
        chunks = self._chunks(parsed.text)
        try:
            spans = source_spans(parsed)
            banks = span_batches(spans, len(chunks))
        except Exception:
            spans, banks = {}, [[] for _ in chunks]
        for chunk, bank in zip(chunks, banks):
            table_pages = set(self._table_pages(parsed, chunk))
            present = {span['source_id'] for span in bank}
            bank.extend(span for key, span in spans.items()
                        if span['page'] in table_pages and key not in present)
            # References share existing writing calls and a bounded context;
            # they must not force extra chunks or an oversized model request.
            limit = max(4000, min(self.config.max_chunk_chars, 16000))
            retained, size = [], 0
            for span in sorted(bank, key=lambda s: s['page'] not in table_pages):
                if size + len(span['quote']) <= limit:
                    retained.append(span)
                    size += len(span['quote'])
            bank[:] = retained
        return chunks, banks, spans

    @staticmethod
    def _table_pages(parsed: ParsedPaper, chunk: str) -> list[int]:
        # Only captions, not a prose mention of a table, bind parser text to
        # physical pages. More than one table can share a source page.
        numbers = {int(match[1]) for match in re.finditer(
            r"(?im)^(?:#{1,6}\s*)?Table\s+(\d+)\s*[:：.]", chunk)}
        try:
            return sorted({item['page'] for item in source_table_inventory(parsed)
                           if item['number'] in numbers})
        except Exception:
            return []

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
