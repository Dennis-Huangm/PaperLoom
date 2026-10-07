"""Resolve verbatim quotations against physical PDF pages, never model page numbers."""
from __future__ import annotations

import re
import unicodedata

from .models import ParsedPaper
from .source_spans import source_spans, exact_span, ID_TOKEN, SPAN_VERSION
from .table_schema import column_role


EVIDENCE_GUIDANCE = """出处引用是附加阅读信息。在重要陈述旁尽量保留提供的原文片段 ID；
没有 ID 时可以使用 [[证据:逐字复制的连续原文摘录]]，不要拼接或猜测原文。
没有合适出处时正常写作并省略引用，不添加“待核对”“引用冲突”“缺少可定位原文依据”等处理提示。
不自行编造引用编号或 PDF 页码；程序会为可定位的出处生成链接。"""


KEY_SECTIONS = ("核心方法", "主要贡献", "实验设置", "关键结果", "局限性")
TOKEN = re.compile(r"\[\[证据:(.*?)\]\]|\[\[证据ID:((?:(?!\[\[)[^\n])*?)\]\]", re.S)
TABLE_CAPTION = re.compile(r"(?:\b(?=Table\s+\d+\s*[:：])|^[ \t]*)Table\s+(\d+)\s*[:：.]\s*([^\r\n]*)", re.I | re.M)


def source_table_inventory(parsed: ParsedPaper | None) -> list[dict]:
    """List PDF table captions with physical pages, independent of model notes."""
    found = {}
    for page, content in enumerate(parsed.page_texts if parsed else [], 1):
        for match in TABLE_CAPTION.finditer(content):
            number = int(match[1])
            found.setdefault(number, {"number": number, "page": page,
                                      "title": " ".join(match[2].split())[:160],
                                      "kind": ('visual' if re.search(r'qualitative\s+(?:examples|results)|定性(?:示例|样例)', match[2], re.I)
                                               else 'matrix')})
    return [found[number] for number in sorted(found)]


def report_table_coverage(report: str, inventory: list[dict]) -> dict:
    """Count reproduced Markdown tables, not captions or images alone."""
    from .table_quality import tables
    headings = list(re.finditer(r"(?m)^#{2,6}\s+[^\n]+", report))
    presented = set()
    partial = set()
    for row in tables(report, TOKEN):
        preceding = next((heading for heading in reversed(headings)
                          if heading.start() < row["header_start"]), None)
        if preceding and (match := re.search(r"\bTable\s+(\d+)\b|表\s*(\d+)\b", preceding[0], re.I)):
            number = int(match[1] or match[2])
            (partial if re.search(r"节选|摘录|excerpt|partial", preceding[0], re.I)
             else presented).add(number)
    known = {item["number"] for item in inventory}
    visual = [item for item in inventory if item.get('kind') == 'visual']
    visual_numbers = {item['number'] for item in visual}
    partial -= presented
    return {"source_count": len(inventory), "presented": sorted(presented & known),
            "partial": [item for item in inventory if item["number"] in partial],
            "missing": [item for item in inventory if item["number"] not in presented | partial | visual_numbers],
            "visual": visual}


def source_table_index(coverage: dict) -> str:
    missing = coverage["missing"]
    partial = coverage.get("partial", [])
    if not missing and not partial:
        return ""
    lines = ["## 原文表格索引", "", "下列原文表格尚未确认完整复刻；未按表号匹配的表也可能以其他标题出现。链接仅指向原文位置，数值未逐项核验。", ""]
    lines.extend(f'- [Table {item["number"]}](paper.pdf#page={item["page"]})：部分摘录；{item["title"]}'
                 for item in partial)
    lines.extend(f'- [Table {item["number"]}](paper.pdf#page={item["page"]})：正文未按表号匹配；{item["title"]}'
                 for item in missing)
    return "\n".join(lines)


def restore_table_row_citations(report: str, notes: list[str], parsed: ParsedPaper,
                                spans: dict[str, dict]) -> str:
    """Restore a dropped row ID only when the note and physical PDF agree.

    A cited peer ties the report table to the same PDF table. Plain PDF text
    also needs an explicit table caption because its headers cannot be bound.
    """
    from .table_quality import check_row, label, scalar, tables

    if not parsed.page_texts or not spans:
        return report
    rows = list(tables(report, TOKEN))
    candidates = {}

    def row_key(row):
        if label(row["headers"][-1]) not in {"依据", "原文依据"}:
            return None
        values = [" ".join(cell[0].split()) for cell in row["cells"][:-1]]
        return tuple(values) if any(scalar(value) is not None for value in values) else None

    def sources(row):
        _, start, end = row["cells"][-1]
        return [spans[key] for match in TOKEN.finditer(row["raw"][start:end])
                if (key := (match.group(2) or "").strip()) in spans]

    def checked_location(row, source_rows):
        binding = check_row(row, source_rows, parsed.page_texts)
        locations = {tuple(item) for item in binding.get("source_rows", [])}
        if binding["status"] not in {"headers_checked", "row_order_checked"} or len(locations) != 1:
            return None
        return binding["status"], next(iter(locations))

    def caption(page, offset):
        prefix = parsed.page_texts[page - 1][:offset]
        matches = list(re.finditer(r"(?im)^\s*(?:table|表)\s*\d+\s*[:：]", prefix))
        return matches[-1].start() if matches else None

    for note in notes:
        for row in tables(note, TOKEN):
            key = row_key(row)
            if key and (found := sources(row)):
                candidates.setdefault(key, []).append(found)

    anchors = {}
    for row in rows:
        if (found := sources(row)) and (location := checked_location(row, found)):
            anchors.setdefault(row["header_start"], []).append(location)

    edits = []
    for row in rows:
        key = row_key(row)
        if not key or len(candidates.get(key, [])) != 1:
            continue
        evidence_cell, start, end = row["cells"][-1]
        if evidence_cell not in {"", "—", "当前材料缺少可定位原文依据"}:
            continue
        found = candidates[key][0]
        checked = checked_location(row, found)
        if not checked:
            continue
        status, (page, offset, _) = checked
        peers = anchors.get(row["header_start"], [])
        table_caption = caption(page, offset)
        if not any(peer_page == page and table_caption == caption(page, peer_start) and
                   (table_caption is not None or status == "headers_checked")
                   for _, (peer_page, peer_start, _) in peers):
            continue
        tokens = " ".join(f'[[证据ID:{source["source_id"]}]]' for source in found)
        edits.append((row["start"] + start, row["start"] + end, " " + tokens + " "))
    for start, end, replacement in reversed(edits):
        report = report[:start] + replacement + report[end:]
    return report


def _normalize(text: str) -> str:
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", text))


def attach_evidence(report: str, parsed: ParsedPaper | None, *, pdf_available: bool,
                    full_report: bool) -> tuple[str, dict]:
    """Compatibility entry point for optional source links; no content audit."""
    from .report_citations import attach_citations
    return attach_citations(report, parsed, pdf_available=pdf_available, full_report=full_report)
