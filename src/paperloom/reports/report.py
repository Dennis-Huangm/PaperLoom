from __future__ import annotations

import re
from contextlib import nullcontext
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from contextvars import copy_context
from functools import partial

from ..config import LLMConfig
from ..runtime.model_access.llm import LLMClient
from ..papers.models import FigureCandidate, Paper, ParsedPaper, VerifiedMetadata
from ..utils import normalize_space
from ..runtime.jobs.task_runtime import TaskCancelled, task_warning, task_progress, task_checkpoint
from .report_checkpoint import CheckpointWriteError, current_report_checkpoint, file_digest
from .evidence import EVIDENCE_GUIDANCE, source_table_inventory
from .report_citations import strip_tokens
from .quality import QUALITY_GUIDANCE
from ..sources.source_spans import (
    source_spans,
    span_batches,
    span_material,
    cited_span_material,
    ground_note_quotes,
    normalize_grouped_source_ids,
    ID_GUIDANCE,
    SYNTHESIS_ID_GUIDANCE,
)
from .report_metadata import metadata_link, protect_metadata
from .report_completeness import normalize_table_titles
from .tables.report_tables import ReportTables, GeneratedReport
from .report_figures import experiment_material, render_experiment_figures


CHUNK_SYSTEM = """你是严谨的 AI 论文阅读助手。仅依据提供的论文片段抽取信息，不得补写不存在的结论。
保留数据集、指标、模型、公式和数值的原名；保留连续原文摘录以便后续定位。使用清楚的中文，压缩重复表述而非方法细节。
方法证据按原文模块或步骤分别记录：输入及来源 → 实际操作与中间表示 → 输出及接收它的下一步，并保留作者说明的设计目的。保留分支、循环、反馈、选择条件及训练与推理的差异；跨片段的依赖保留原名和原文章节线索，未在本片段说明的连接保持未知。
核心改动展开到实际计算或处理动作，不能把具体操作缩写成“融合特征”“优化”“增强表示”等功能标签。常规组件简述其在本文中的作用。方法细节在附录、图注或算法中出现时同样提取，不因实验表格较多而省略。
单独保留原文案例中贯穿方法的输入、中间状态和输出，注明它是案例还是实验结果；没有案例时不在抽取阶段创造示例。为方法操作、公式和案例保留对应原文摘录及证据 ID，供合成阶段连接不同片段。
如果片段包含关键公式（包括附录中的指标定义），必须同时提取公式、符号定义、它连接的输入输出以及作者给出的设计目的。
保留本片段出现的作者机构、代码/数据集/项目链接和复现参数，不要因其位于脚注或附录而省略。
逐表保留实验表格，使用含原文 Table 编号的标题和 Markdown 表格，保留各模型行、表头、条件、单位与脚注；不要只摘最优结果。逐字保留原表列名和模型行标签，包括名称的缩写，不扩展版本后缀；中文解释写在表格外。跨片段表格不猜测缺失部分。
原文表格与正文数值不一致时，矩阵内逐字保留原表数值，在表注单独说明差异；不得用正文、常识或其他版本的值“纠正”原表，不能添加原表没有的星号。原表空白单元格保持为空；若题注规定空白沿用基线，则在表注完整保留该规则，不写成“未提供”、不填充推测值。原表未命名的行标签列保留空表头，不另造“变体行”或“条件/变体”等标题。
数学表头及数值必须保留上下标和数量级，用 LaTeX 或 Unicode 上下标表达。例如参数量单位 ×10⁶ 不能平铺成 ×106，FLOPs 的 10¹⁸ 不能写成 1018；对照解析正文中的数学标记与原文上下文，不能因 PDF 文本平铺而丢失幂次。
表格说明与脚注用自然中文解释具体含义，明确规模、单位、指标方向和比较条件；保留必要英文列名，不能只贴英文表注或生硬逐词翻译。
每张表只写一个带原表号的标题和一段中文表注；不另写“原文标题”“表注说明”等重复包装，不把标题再复制到表注。中文表注完整保留单位、条件、符号和脚注，但同一含义只解释一次；结果趋势分析放在正文，不在表注中重复。
逐图保留正文实验图的原 Figure 编号、实验条件、比较对象、结果趋势及作者分析，不因图像无法解析而省略正文支持的结果。
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
- “关键模块与流程”按实际依赖关系展开：每个关键模块说明输入从哪里来、具体做了哪些操作、产生什么中间结果、交给哪个后续步骤，以及作者给出的设计目的。连接跨片段的同名模块，保留决定方法行为的分支、循环、反馈和选择条件，使读者能解释这一步怎样改变输入及下一步怎样使用其结果；
- 常规组件简述作用，作者的核心改动展开实际机制；仅写“融合特征”“优化表示”等效果描述不算解释操作。依论文复杂度决定模块数量和篇幅，不能为简短而删去关键操作，也不强行为所有论文套用训练流程；
- 如果证据中存在关键公式，逐个保留并放在对应步骤附近解析：定义所有主要符号，解释计算顺序与输入输出，说明设计动机、优化目标，以及它如何影响训练信号、推理决策或最终结果；
- 公式解析必须紧跟对应公式，不得只翻译公式前后的原文，也不得根据常识虚构论文没有给出的公式、符号含义或因果结论；
- 模块展开后提供简短“流程走读”。先用一句话给出可辨认的具体输入内容，再沿同一输入解释各步骤的中间状态和最终输出；只把 T、x 等变量代入模块名称或重述总流程不算走读。小节开头明确写“论文案例”并给出原文出处，或写“教学示例（非论文实验结果）”；优先采用原文案例，缺少完整案例时使用后者，不能把泛称的“典型输入”冒充论文案例。教学示例仅将已有机制实例化，不虚构数值结果、实现细节或论文未给出的操作。材料不足以串起某步时，就地说明“当前材料未能确认”的具体连接，保留未知；
- 解释深度来自原文支持的操作与依赖，不能用猜测补齐细节：原文只称“占位符”时保留该称呼，不自行指定形状或数据结构；未找到训练说明时，区分“已描述的推理流程”和“未能确认的训练安排”，不能据此断言无需训练。分析性解释与作者明确给出的设计目的分别表述；
- 正文即使不读图下注解也应能读懂完整方法链。可引用 Figure 编号对应模块，允许讲解必需的信息重合；图下注解负责图中框、箭头和符号的对应，不能以“见图”替代正文机制解释。AI 图解仅作辅助线索，具体机制以原文正文、公式、算法和图注为依据；
- 使用“方法总览”“关键模块与流程”“流程走读”等三级标题组织阅读；公式与解释就近放置，避免另起小节重复讲一遍流程。若论文没有关键公式，则自然省略，不得生成占位说明。"""


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
    from .tables.table_quality import normalize_table_separators
    from .evidence import TOKEN
    markdown_text = normalize_table_separators(markdown_text, TOKEN)
    markdown_text = normalize_table_titles(markdown_text)
    markdown_text = _repair_inline_canonical_headings(markdown_text)
    figures = (
        main_figure
        if isinstance(main_figure, list)
        else ([main_figure] if main_figure is not None else [])
    )
    method_figures = [f for f in figures if f.role == "method" and f.image_available]
    markdown_text = re.sub(r"(?s)<!-- method-figures -->.*?<!-- /method-figures -->", "", markdown_text)
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
        figure_block = "<!-- method-figures -->\n\n" + "\n\n".join(part for part in blocks if part).strip() + "\n\n<!-- /method-figures -->"
        if re.search(r"(?m)^##\s+核心方法\s*$", markdown_text):
            markdown_text = re.sub(
                r"(?m)^(##\s+核心方法\s*)$",
                lambda match: f"{match.group(1)}\n\n{figure_block.rstrip()}\n\n",
                markdown_text,
                count=1,
            )
    markdown_text = render_experiment_figures(markdown_text, figures)
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
        with checkpoint.request_stage(key) if checkpoint else nullcontext():
            if key == 'table-organization-v1':
                result = self.llm.chat(system, user, retry_on_error=False)
            else:
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
        report_figures = (
            main_figure if isinstance(main_figure, list) else ([main_figure] if main_figure else [])
        )
        checkpoint = current_report_checkpoint()
        if checkpoint:
            checkpoint.configure_model(self.config, self.llm)
        chunks, banks, spans = self.analysis_inputs(parsed)
        evidence_notes = self._analyze_material(paper, parsed, report_figures, chunks, banks)
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
            from .tables.table_organization import organize_tables
            table_catalogue = organize_tables(evidence_notes, table_inventory, parsed, self._chat)
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
                for figure in report_figures if figure.role == "method"
            )
            if any(figure.role == "method" for figure in report_figures)
            else "没有可靠方法图；跳过方法图解析。"
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

## 方法图材料（正文吸收原文支持的机制与连接，图示小节由程序生成）
{figure_text}

正文须结合原文图注与分片证据讲清方法，不因图下注解已提及某一步就省略它。AI 图示解读不是独立事实依据；只用它帮助对应模块，不据此补造原文未说明的机制。不要重复创建图片链接或逐图讲解小节。

## 实验图材料（按原文顺序，全部保留）
{experiment_material(report_figures)}

逐张使用独立一行的 `[[实验图:figure-N]]` 在对应实验讨论中选择展示位置，N 必须来自上面的材料。
每张图只展示一次，其他地方用原 Figure 编号引用。混合设置与结果的图保留完整图，按主要用途放置。
设置、数据与评测流程图放“实验设置”，结果对比、定性案例、消融图放“关键结果”。各章节内实验主题沿用原论文顺序。
图的位置紧邻对应分析，解释实验条件、比较对象、趋势、结论及限制，不要只有图而没有实验分析。
程序负责插入原图及图示解读，不要重复生成图片链接或复述已有图示解读；正文负责解释实验意义。
即使图片提取或视觉解读失败，也保留原文图注与正文支持的实验分析，不猜测视觉细节。

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
6. 方法图由后续定稿器统一插入；不得输出“方法图解析”“主图说明”或在核心方法中创建 Figure/Fig. 编号小节。实验图使用上面的实验图位置指令，按实验主题组织分析。
7. 如果没有可靠方法图，跳过方法图内容，不得写“未提取到主图”等提示；不影响实验图及实验分析。
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
20. 善用 Markdown **加粗**帮助读者扫读长段落：优先突出核心机制、模块的关键操作或中间结果、与基线的关键区别、实验结论及影响结论的限制条件。较长段落通常选择 1–2 个有信息量的短语，短段落无需强行加粗；重点应回答“做了什么、为何关键、结果说明什么”，不要只加粗模型名称、泛泛标签或孤立数值。流程走读突出各步实际发生的变化。保持正文连贯，避免整句整段加粗、连续堆叠重点或重复强调同一术语；公式、代码、引用标记及表格原始数据不嵌入强调符号。使用标准 **短语** 语法，不输出自定义高亮标签或颜色。
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

    def _analyze_material(self, paper, parsed, figures, chunks, banks):
        checkpoint = current_report_checkpoint()
        if checkpoint:
            checkpoint.publish(chunks_total=len(chunks), chunks_done=0)
        notes = [""] * len(chunks)
        completed = 0
        tasks = []
        # Interleave both kinds so a long figure does not hold back all text.
        for index in range(max(len(figures), len(chunks))):
            if index < len(figures):
                tasks.append(("figure", index, partial(self._explain_figure, index, paper, figures[index], parsed)))
            if index < len(chunks):
                tasks.append(("chunk", index, partial(self._analyze_chunk, index + 1,
                    chunks[index], banks[index], parsed, len(chunks))))

        def completed_result(kind, index, result):
            nonlocal completed
            if kind == "chunk":
                notes[index] = result
                completed += 1
                if checkpoint:
                    checkpoint.publish(chunks_done=completed)
            task_progress(f"图示与正文分析：已完成正文分片 {completed}/{len(chunks)}…",
                          65 + round(17 * completed / len(chunks)))

        workers = min(self.config.analysis_workers, len(tasks))
        if workers == 1:
            for kind, index, operation in tasks:
                task_checkpoint()
                completed_result(kind, index, operation())
            return notes

        remaining = iter(tasks)
        executor = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="report-analysis")
        pending = {}

        def submit_next():
            task_checkpoint()
            item = next(remaining, None)
            if item is not None:
                kind, index, operation = item
                pending[executor.submit(copy_context().run, operation)] = (kind, index)

        try:
            for _ in range(workers):
                submit_next()
            while pending:
                task_checkpoint()
                done, _ = wait(pending, timeout=0.1, return_when=FIRST_COMPLETED)
                for future in done:
                    kind, index = pending.pop(future)
                    completed_result(kind, index, future.result())
                # Inspect the whole completed batch before starting more work.
                for _ in done:
                    submit_next()
        finally:
            # In-flight responses save their own checkpoints before we leave
            # the task/budget context; no queued work survives an error/cancel.
            executor.shutdown(wait=True, cancel_futures=True)
            try:
                task_checkpoint()
            except TaskCancelled:
                pass
            else:
                # A failed sibling must not hide other drained successes from
                # the resumable progress count. Cancellation keeps their files.
                for future, (kind, index) in pending.items():
                    if not future.cancelled() and future.exception() is None:
                        completed_result(kind, index, future.result())
        return notes

    def _analyze_chunk(self, index, chunk, bank, parsed, total):
        table_pages = self._table_pages(parsed, chunk)
        table_material = "\n\n".join(
            f"[PDF 物理第 {page} 页，完整页面文本]\n{parsed.page_texts[page - 1]}"
            for page in table_pages
        ) or "本分片没有可按原表题对应的独立 PDF 表格页面。"
        return self._chat(
            f"chunk-{index}",
            CHUNK_SYSTEM + "\n" + EVIDENCE_GUIDANCE + "\n" + ID_GUIDANCE + "\n" + QUALITY_GUIDANCE,
            f"""这是论文第 {index}/{total} 个片段：

{chunk}

独立 PDF 原文片段（可能与解析正文分片不同；同时提取这里的证据，引用实际 ID）：
{span_material(bank)}

本分片表格对应的完整 PDF 物理页（独立于 Docling 的列划分）：
{table_material}

请提取：研究问题、方法机制、关键公式及符号定义、主要贡献、实验设置、关键结果与数值、作者明确陈述的局限性。
没有出现的项目写“本片段未出现”，不要把 future work 自动当成局限性。
另保留资源链接及附录中的指标定义；表格行与列归属不清楚时标为待核对，不猜测。
解析正文的表头、列数或行列归属与独立 PDF 页面不同，应回查完整页面及其证据 ID，不能把解析器多出的列、重复单元格或分片边界当作原文缺失。
同一原表的全部模型、难度和指标应保留，不能把完整数值行压缩成无列归属的数值串。""",
        )

    def _explain_figure(self, index, paper, figure, parsed):
        checkpoint = current_report_checkpoint()
        if figure.explanation:
            return
        context = ""
        if parsed and figure.role == "experiment":
            if 0 < figure.page <= len(parsed.page_texts):
                context = parsed.page_texts[figure.page - 1][:10000]
            else:
                context = "\n".join(page for page in parsed.page_texts if figure.caption[:30] in normalize_space(page))[:10000]
        inputs = ["figure-explanation-v2", file_digest(figure.path) if figure.image_available else "missing",
                  paper.title, paper.abstract, figure.caption, figure.role, context] if checkpoint else None
        cached = checkpoint.get(f"figure-{index}", inputs) if checkpoint else None
        if cached:
            figure.explanation = cached
            return
        task_checkpoint()
        try:
            if not figure.image_available:
                raise ValueError("image unavailable")
            options = {"role": "experiment", "context": context} if figure.role == "experiment" else {}
            with checkpoint.request_stage(f"figure-{index}") if checkpoint else nullcontext():
                figure.explanation = self.llm.describe_figure(
                    figure.path, paper.title, paper.abstract, figure.caption, **options,
                )
            if not figure.explanation.strip():
                raise ValueError("empty figure explanation")
        except Exception as vision_exc:
            try:
                guidance = ("解释实验条件、坐标或子图含义、比较对象、趋势和结论边界。只依据图注和正文，不猜测视觉细节。"
                            if figure.role == "experiment" else "解释图的输入、关键步骤、输出和核心含义。")
                with checkpoint.request_stage(f"figure-{index}-text") if checkpoint else nullcontext():
                    figure.explanation = self.llm.chat(
                        "你是论文图示讲解助手。仅依据提供的原文，不猜测。",
                        f"""论文：{paper.title}
摘要：{paper.abstract[:1200]}
原始 caption：{figure.caption}

原文上下文：{context}
请用 2-4 句通俗中文{guidance}""",
                    ).strip()
                if figure.role == "experiment":
                    figure.explanation = "（依据原文图注与正文，未作视觉解读。）" + (figure.explanation or figure.caption)
            except Exception as text_exc:
                task_warning(
                    "LLM 图片解读",
                    "图示的视觉与文本解读均失败，报告将保留原始图注。"
                    f"请检查模型是否支持图片输入（{type(vision_exc).__name__}; {type(text_exc).__name__}）。",
                )
                figure.explanation = ("（保留原文图注，未作视觉解读。）" if figure.role == "experiment" else "") + figure.caption
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
| 链接 | {metadata_link(paper.abs_url)} |
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
