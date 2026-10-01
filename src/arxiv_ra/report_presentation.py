"""Compact the generated audit appendices of historical reading artifacts.

Source Markdown and JSON remain intact; this changes only their HTML view.
"""
from __future__ import annotations

import re


def compact_report(markdown_text: str) -> str:
    # Only recognize real level-two headings, not examples inside code fences.
    headings = []
    offset = 0
    fence = None
    for line in markdown_text.splitlines(keepends=True):
        marker = re.match(r"^ {0,3}(`{3,}|~{3,})", line)
        if fence:
            if marker and marker[1][0] == fence[0] and len(marker[1]) >= len(fence):
                fence = None
        elif marker:
            fence = marker[1]
        elif match := re.match(r"^##[ \t]+([^\r\n]+)", line):
            headings.append((offset, match[1].strip()))
        offset += len(line)
    links = {}
    parts = []
    cursor = 0
    for i, (start, title) in enumerate(headings):
        end = headings[i + 1][0] if i + 1 < len(headings) else len(markdown_text)
        section = markdown_text[start:end]
        replacement = None
        if title == "条目依据与待核对原因":
            replacement = ""
        elif title == "固定证据摘录":
            entries = list(re.finditer(r'<h3 id="(p\d+-[a-z0-9]+)">[^<]+</h3>', section))
            for j, entry in enumerate(entries):
                stop = entries[j + 1].start() if j + 1 < len(entries) else len(section)
                block = section[entry.end():stop]
                # Source links emitted by the comparison writer are on their
                # own lines; links inside quoted model prose are not trusted.
                link = re.search(r"(?m)^\[(?:PDF 第 \d+ 页|打开所用阅读报告)\]\(([^\s)]+)\)", block)
                links[entry[1]] = link[1] if link else "sources.json"
            replacement = "[查看固定来源快照](sources.json) · [查看条目核对详情](matrix.json)\n\n"
        elif title == "原文依据与覆盖":
            # Preserve coverage limitations; omit implementation exposition,
            # per-claim copies and the entire duplicate quotation appendix.
            lead = section.split("###", 1)[0]
            summary = [line for line in lead.splitlines() if line.startswith((
                "用于定位的文本包含", "已定位 ", "本报告缺少", "PDF 仅处理", "尚无已定位", "另有 "))]
            summary.append("引用链接仅核验原文位置，不代表结论正确。[查看引用与数值核对详情](evidence.json)。")
            replacement = "## 引用与核对\n\n" + "\n\n".join(summary) + "\n\n"
        if replacement is not None:
            parts.extend((markdown_text[cursor:start], replacement))
            cursor = end
    parts.append(markdown_text[cursor:])
    result = "".join(parts)
    for anchor, target in links.items():
        result = result.replace(f"](#{anchor})", f"]({target})")
    result = result.replace("请先查看文末“实验数值核对”，再使用这些结果。",
                            "使用前请[核对原文与数值](evidence.json)。")
    # Keep references compact; page information belongs in the hover title.
    return re.sub(r'\[(?:原文第 \d+ 页 · 引用 )?(\d+)\]\(paper\.pdf#page=(\d+)\)',
                  lambda m: f'[{m[1]}](paper.pdf#page={m[2]} "原文第 {m[2]} 页")', result)
