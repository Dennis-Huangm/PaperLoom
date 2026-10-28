"""Experiment figure slots keep illustrations beside the discussion that owns them."""
from __future__ import annotations

import re

from ..papers.models import FigureCandidate


def figure_number(figure: FigureCandidate) -> int:
    match = re.match(r"(?:Figure|Fig\.?)\s*(\d+)", figure.caption, re.I)
    return int(match.group(1)) if match else 10_000 + figure.page


def experiment_material(figures: list[FigureCandidate]) -> str:
    return "\n\n".join(
        f"[[实验图:figure-{figure_number(f)}]]\n原文章节：{f.section}\n{f.caption}\n{f.explanation}"
        for f in figures if f.role == "experiment"
    ) or "无实验图材料。"


def render_experiment_figures(report: str, figures: list[FigureCandidate]) -> str:
    figures = [f for f in figures if f.role == "experiment"]
    if not figures:
        return report
    # Restore owned blocks to slots before rendering again; prose stays untouched.
    report = re.sub(
        r"(?s)<!-- experiment-figure:(\d+) -->.*?<!-- /experiment-figure -->",
        lambda m: f"[[实验图:figure-{m[1]}]]", report,
    )
    for figure in figures:
        number = figure_number(figure)
        slot = f"[[实验图:figure-{number}]]"
        block = [f"<!-- experiment-figure:{number} -->", f"#### Figure {number}"]
        if figure.image_available:
            block.append(f"![论文实验图 Figure {number}]({figure.path.name})")
        else:
            block.append("原图未能提取；以下依据原文图注及正文。")
        block.append(figure.explanation or figure.caption)
        block.append("<!-- /experiment-figure -->")
        rendered = "\n\n".join(block)
        # Only a standalone slot in an experiment section may own the image.
        sections = list(re.finditer(r"(?m)^##\s+([^\n]+)\s*$", report))
        placed = False
        for index, heading in enumerate(sections):
            if heading[1].strip() not in ("实验设置", "关键结果"):
                continue
            end = sections[index + 1].start() if index + 1 < len(sections) else len(report)
            content = report[heading.end():end]
            match = re.search(rf"(?m)^{re.escape(slot)}[ \t]*$", content)
            if match:
                start, stop = heading.end() + match.start(), heading.end() + match.end()
                report = report[:start] + rendered + report[stop:]
                placed = True
                break
        if not placed:
            for index, heading in enumerate(sections):
                if heading[1].strip() not in ("实验设置", "关键结果"):
                    continue
                end = sections[index + 1].start() if index + 1 < len(sections) else len(report)
                content = report[heading.end():end]
                mention = re.search(rf"(?:Figure|Fig\.?)\s*{number}(?!\d)", content, re.I)
                if mention:
                    paragraph_end = content.find("\n\n", mention.end())
                    position = heading.end() + (paragraph_end if paragraph_end >= 0 else len(content))
                    report = report[:position].rstrip() + "\n\n" + rendered + "\n\n" + report[position:].lstrip()
                    placed = True
                    break
        if not placed:
            # Missing slots still retain every experiment figure and its analysis.
            target = "实验设置" if re.search(r"setup|protocol|dataset|benchmark construction", figure.section, re.I) else "关键结果"
            match = re.search(rf"(?ms)^##\s+{target}\s*\n.*?(?=^##\s+|\Z)", report)
            if match:
                report = report[:match.end()].rstrip() + "\n\n" + rendered + "\n\n" + report[match.end():]
            else:
                report = report.rstrip() + f"\n\n## {target}\n\n" + rendered + "\n"
        report = report.replace(slot, f"Figure {number}")
    return report
