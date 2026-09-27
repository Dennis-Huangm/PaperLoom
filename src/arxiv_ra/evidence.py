"""Resolve verbatim quotations against physical PDF pages, never model page numbers."""
from __future__ import annotations

import re
import unicodedata

from .models import ParsedPaper
from .quality import audit_report_numbers, apply_numeric_policy
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
        nonlocal rejected
        resolved = resolve(match)
        if resolved is None:
            rejected += 1
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
        return f'[原文 {existing["id"]} · PDF 第 {page} 页](paper.pdf#page={page})'

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
        report = apply_numeric_policy(report, numeric_audit)
    sections = list(re.finditer(r"(?m)^##\s+([^\n]+)", report))
    report = TOKEN.sub(replace, report)
    if numeric_audit["issues"]:
        notice = "> **实验数值待核对**：依据不足或归属冲突的数值已暂不展示；表格中的“—”不代表零或论文未报告。[查看原始内容与核对详情](evidence.json)。\n\n"
        title = re.match(r"\s*# [^\n]+\n", report)
        position = title.end() if title else 0
        report = report[:position] + "\n" + notice + report[position:]
    missing = [s for s in KEY_SECTIONS if s not in covered]
    coverage = {"status": "checked" if full_report and any(normalized) else "unavailable",
                "parsed_pages": len(pages), "total_pages": parsed.total_pages if parsed else None,
                "nonempty_pages": sum(bool(p.strip()) for p in pages),
                "cited_pages": sorted({c["page"] for c in citations}), "validated_citations": len(citations),
                "rejected_citations": rejected, "covered_sections": sorted(covered),
                "missing_sections": missing, "citations": citations, "numeric_audit": numeric_audit}
    coverage["source_spans"] = {"version": SPAN_VERSION, "available": len(spans),
                                "cited": sum("source_id" in c for c in citations)}
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
    if any(c["status"] in {"row_order_checked", "partial_headers_checked", "unassessed"} for c in numeric_audit.get("table_checks", [])):
        summary.append("部分表格尚未完成指标表头、单位和实验条件的对应核验，请结合原文阅读。")
    return report.rstrip() + "\n\n" + "\n\n".join(summary) + "\n", coverage
