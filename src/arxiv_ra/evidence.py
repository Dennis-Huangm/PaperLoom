"""Resolve verbatim quotations against physical PDF pages, never model page numbers."""
from __future__ import annotations

import re
import unicodedata

from .models import ParsedPaper
from .quality import audit_report_numbers, apply_numeric_policy, preserve_unverified_content
from .source_spans import source_spans, exact_span, ID_TOKEN, SPAN_VERSION


def _complete_cited_rows(report, parsed, spans, resolve):
    """Complete an intersecting, uniquely resolved row, not an arbitrary page.

    New precise citations are subject to the existing exact-span bounds and
    normal publication audit. Plain PDF rows retain the weaker order-only
    status; completing a quote does not certify a grouped header.
    """
    from .table_quality import tables, check_row
    from .quality import unsupported_numbers
    edits, completed = [], []
    for row in tables(report, TOKEN):
        sources = [s for m in TOKEN.finditer(row['raw']) if (s := resolve(m))]
        if not sources or not unsupported_numbers(TOKEN.sub('', row['raw']), '\n'.join(s['quote'] for s in sources)):
            continue
        binding = check_row(row, sources, parsed.page_texts)
        locations = {tuple(item) for item in binding.get('source_rows', [])}
        if binding['status'] not in {'headers_checked', 'row_order_checked'} or len(locations) != 1:
            continue
        page, start, end = next(iter(locations))
        # Only offset-backed references can justify bounded expansion.
        source = next((s for s in sources if s['page'] == page and 'start' in s
                       and max(start, s['start']) < min(end, s['end'])), None)
        if not source:
            continue
        key = f'Q1-{source["text_sha256"][:24]}-{page}-{start}-{end}'
        span = exact_span(key, parsed, digest=source['text_sha256'])
        if not span or key in row['raw']:
            continue
        # Append in the existing evidence cell, preserving table width and
        # every original reference. No report values are changed here.
        match = list(TOKEN.finditer(row['raw']))[-1]
        position = row['start'] + match.end()
        edits.append((position, f' [[证据ID:{key}]]'))
        spans[key] = span
        completed.append({'page': page, 'start': start, 'end': end, 'source_id': key,
                          'original_source_ids': [s.get('source_id', '') for s in sources],
                          'binding_status': binding['status']})
    for position, value in reversed(edits):
        report = report[:position] + value + report[position:]
    return report, completed


EVIDENCE_GUIDANCE = """对核心方法、主要贡献、实验设置、关键结果、局限性中的重要陈述，在句末添加
[[证据:逐字复制的连续原文摘录]]，摘录含 20–600 个非空白字符；允许空白变化，不得翻译、拼接或改写。
实验数值所在段落及 Markdown 表格的每个数值行，都须在同段/同行保留含对应数值的摘录；表格可增加“原文依据”列。
只引用表题或定性结论不能支撑表中数字。分片笔记须保留原始表头与各行的对应关系及逐字摘录，最终整合不得丢弃数值依据。
摘录必须连续且位于同一 PDF 物理页面，不能跨页拼接，也不能带自行添加的页码标签或将重排后的 Markdown 表格当作原文。
过短时扩展到相邻的原文上下文；跨页时拆为各自满足长度要求的独立摘录。保留原文标点和断行连字符，不能补写或重复单词。
最终报告必须保留该格式和原文，系统会在实际 PDF 中定位页码。找不到原文时写“缺少可定位原文依据”，
不要自行生成 PDF 链接、证据编号或页码。来源摘录只是核对入口，不等于结论正确。"""

KEY_SECTIONS = ("核心方法", "主要贡献", "实验设置", "关键结果", "局限性")
TOKEN = re.compile(r"\[\[证据:(.*?)\]\]|\[\[证据ID:([^\]\n]*)\]\]", re.S)
TABLE_CAPTION = re.compile(r"\bTable\s+(\d+)\s*[:：]\s*([^\r\n]*)", re.I)


def source_table_inventory(parsed: ParsedPaper | None) -> list[dict]:
    """List PDF table captions with physical pages, independent of model notes."""
    found = {}
    for page, content in enumerate(parsed.page_texts if parsed else [], 1):
        for match in TABLE_CAPTION.finditer(content):
            number = int(match[1])
            found.setdefault(number, {"number": number, "page": page,
                                      "title": " ".join(match[2].split())[:160]})
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
    partial -= presented
    return {"source_count": len(inventory), "presented": sorted(presented & known),
            "partial": [item for item in inventory if item["number"] in partial],
            "missing": [item for item in inventory if item["number"] not in presented | partial]}


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


def publication_gate(coverage: dict) -> dict:
    """Summarize automatic review findings without certifying paper truth."""
    audit = coverage["numeric_audit"]
    issues = audit.get("issues", [])
    conflicts = [issue for issue in issues if issue.get("table_binding", {}).get("bad_columns")]
    unverified = [issue for issue in issues if issue not in conflicts]
    tables = coverage["table_coverage"]
    unassessed_tables = sum(check.get("status") in {"unassessed", "partial_headers_checked", "row_order_checked"}
                            for check in audit.get("table_checks", []))
    # A Markdown table and a matching caption do not establish that every PDF
    # row, condition and footnote was reproduced.
    unassessed_completeness = len(tables["presented"])
    review = (not coverage["status"] == "checked" or bool(issues) or
              bool(tables["missing"]) or
              bool(coverage.get("rejected_prose_citations", coverage["rejected_citations"])))
    return {"status": "review_required" if review else "automatic_checks_passed",
            "source_located_citations": coverage["validated_citations"],
            "unverified_claims": len(unverified), "conflicting_claims": len(conflicts),
            "partial_tables": len(tables["partial"]), "missing_tables": len(tables["missing"]),
            "table_completeness_unassessed": unassessed_completeness,
            "unassessed_table_rows": unassessed_tables,
            "scope": "source_location_and_limited_numeric_consistency; not_semantic_or_paper_truth"}


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
    pages = parsed.page_texts if parsed and pdf_available else []
    normalized = [_normalize(page) for page in pages]
    spans = source_spans(parsed) if full_report and pdf_available else {}
    if full_report and pdf_available:
        for token in ID_TOKEN.finditer(report):
            key = token[1].strip()
            if key not in spans and (span := exact_span(key, parsed)):
                spans[key] = span
    citations: list[dict] = []
    rejected = 0
    rejected_prose = 0
    table_ranges = []
    covered: set[str] = set()

    def resolve(match):
        if match.group(2) is not None:
            return spans.get(match.group(2).strip())
        quote = " ".join(match.group(1).split())
        needle = _normalize(quote)
        matches = [i + 1 for i, page in enumerate(normalized) if needle and needle in page]
        if full_report and 20 <= len(needle) <= 600 and len(matches) == 1:
            return {"page": matches[0], "quote": quote}
        return None

    def replace(match: re.Match) -> str:
        nonlocal rejected, rejected_prose
        resolved = resolve(match)
        if resolved is None:
            rejected += 1
            if any(start <= match.start() < end for start, end in table_ranges):
                return "—"
            rejected_prose += 1
            return "（原文摘录未能唯一定位，请核对）"
        quote = " ".join(resolved["quote"].split())
        page = resolved["page"]
        section = next((s.group(1).strip() for s in reversed(sections) if s.start() < match.start()), "")
        if section in KEY_SECTIONS:
            covered.add(section)
        key = (page, quote)
        existing = next((c for c in citations if (c["page"], c["quote"]) == key), None)
        if existing is None:
            existing = {"id": len(citations) + 1, "page": page, "quote": quote, "sections": []}
            citations.append(existing)
        if resolved.get("source_id"):
            existing.update({k: resolved[k] for k in ("source_id", "start", "end", "text_sha256")})
        if section and section not in existing["sections"]:
            existing["sections"].append(section)
        return f'[{existing["id"]}](paper.pdf#page={page})'

    report = re.sub(r"\[([^\]\n]+)\]\([^\s)]*#page=\d+[^)]*\)", r"\1（模型提供的页码未核对）", report, flags=re.I)
    # Covers raw HTML, reference-style links and bare URLs too. Only the
    # deterministic replacement below may introduce PDF page fragments.
    report = re.sub(r"#page\s*=\s*\d+", "#unverified-location", report, flags=re.I)
    completed_rows = []
    if full_report and pages:
        report, completed_rows = _complete_cited_rows(report, parsed, spans, resolve)
    sections = list(re.finditer(r"(?m)^##\s+([^\n]+)", report))
    def is_located(quote):
        needle = _normalize(quote)
        return full_report and 20 <= len(needle) <= 600 and sum(needle in page for page in normalized) == 1
    numeric_audit = (audit_report_numbers(report, TOKEN, is_located,
                       resolve_quote=lambda m: (resolve(m) or {}).get("quote"),
                       resolve_source=resolve, pages=pages) if full_report else
                     {"version": 1, "checked_claims": 0, "issues": [], "semantic_support": "not_assessed"})
    numeric_audit['completed_row_citations'] = completed_rows
    if full_report:
        numeric_audit = preserve_unverified_content(numeric_audit, TOKEN, quiet_tables=True)
        for issue in numeric_audit["issues"]:
            issue["review_state"] = ("explicit_conflict" if issue.get("table_binding", {}).get("bad_columns")
                                     else "pending_verification")
        report = apply_numeric_policy(report, numeric_audit)
    from .table_quality import tables
    table_ranges = [(row['start'], row['end']) for row in tables(report, TOKEN)]
    # Earlier synthesis can replace unavailable IDs with verbose placeholders.
    # Clear those inside tables only; prose uncertainty remains visible.
    for start, end in reversed(table_ranges):
        row = re.sub(r'（(?:片段引用不可用|分片摘录未能唯一定位)，缺少可定位原文依据）', '—', report[start:end])
        report = report[:start] + row + report[end:]
    table_ranges = [(row['start'], row['end']) for row in tables(report, TOKEN)]
    sections = list(re.finditer(r"(?m)^##\s+([^\n]+)", report))
    report = TOKEN.sub(replace, report)
    if numeric_audit["issues"]:
        notice = "> **实验数值待核对**：标注“待核对”的内容尚未确认，请勿作为已核实结论；明确归属冲突的单元格以“—”显示，不代表零或论文未报告。[查看核对详情](evidence.json)。\n\n"
        title = re.match(r"\s*# [^\n]+\n", report)
        position = title.end() if title else 0
        report = report[:position] + "\n" + notice + report[position:]
    missing = [s for s in KEY_SECTIONS if s not in covered]
    coverage = {"status": "checked" if full_report and any(normalized) else "unavailable",
                "parsed_pages": len(pages), "total_pages": parsed.total_pages if parsed else None,
                "nonempty_pages": sum(bool(p.strip()) for p in pages),
                "cited_pages": sorted({c["page"] for c in citations}), "validated_citations": len(citations),
                "rejected_citations": rejected, "rejected_prose_citations": rejected_prose,
                "covered_sections": sorted(covered),
                "missing_sections": missing, "citations": citations, "numeric_audit": numeric_audit}
    coverage["source_spans"] = {"version": SPAN_VERSION, "available": len(spans),
                                "cited": sum("source_id" in c for c in citations)}
    coverage["table_coverage"] = report_table_coverage(report, source_table_inventory(parsed) if full_report else [])
    coverage["publication_gate"] = publication_gate(coverage)
    total = coverage["total_pages"]
    summary = ["## 引用与核对"]
    if coverage["status"] == "unavailable":
        summary.append("本报告缺少可用于全文引用校验的 PDF 正文，尚无已定位的原文依据。")
    else:
        summary.append(f"用于定位的文本包含 {len(pages)} 页（PDF 共 {total if total is not None else '未知'} 页），"
                       f"其中 {coverage['nonempty_pages']} 页有可提取文字；已定位 {len(citations)} 条摘录，涉及 {len(coverage['cited_pages'])} 页。")
        if total and len(pages) < total:
            summary.append("PDF 仅处理了部分页面，未处理页面不在此次校验范围内。")
        if missing:
            summary.append("尚无已定位摘录的关键章节：" + "、".join(missing) + "。")
        if rejected:
            summary.append(f"另有 {rejected} 条摘录因过短、过长、未匹配或匹配多页而未生成链接。")
    summary.append("引用链接仅核验原文位置，不代表结论正确。[查看引用与数值核对详情](evidence.json)。")
    if table_index := source_table_index(coverage["table_coverage"]):
        summary.append(table_index)
    return report.rstrip() + "\n\n" + "\n\n".join(summary) + "\n", coverage
