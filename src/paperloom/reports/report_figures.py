"""Experiment figure slots keep illustrations beside the discussion that owns them."""
from __future__ import annotations

from dataclasses import replace
from html import escape
import re

from ..papers.models import FigureCandidate


def figure_number(figure: FigureCandidate) -> int:
    match = re.match(r"(?:Figure|Fig\.?)\s*(\d+)", figure.caption, re.I)
    return int(match.group(1)) if match else 10_000 + figure.page


def merge_report_figures(
    pdf_figures: list[FigureCandidate], html_figures: list[FigureCandidate],
) -> list[FigureCandidate]:
    by_number = {figure_number(figure): figure for figure in pdf_figures}
    for html in html_figures:
        number = figure_number(html)
        pdf = by_number.get(number)
        if pdf is None:
            by_number[number] = html
            continue
        # A float can sit under different headings across versions of the layout.
        # Keep experiment attribution independently of the selected image.
        location = html if html.role == "experiment" and pdf.role != "experiment" else pdf
        image = html if html.image_available else pdf
        by_number[number] = replace(image, page=pdf.page, role=location.role, section=location.section)
    return [by_number[number] for number in sorted(by_number)]


def experiment_material(figures: list[FigureCandidate]) -> str:
    """Give the writer placement cues. Detailed readings belong to the figure block."""
    return "\n\n".join(
        f"图 {figure_number(f)}（{f.section}）：{f.caption.split('. ')[0][:180]}"
        for f in figures if f.role == "experiment"
    ) or "无实验图材料。"


def expand_experiment_groups(report: str, figures: list[FigureCandidate]) -> str:
    """Project figure groups to slots and one optional cross-figure finding."""
    known = {figure_number(f) for f in figures if f.role == "experiment"}
    if not known:
        return report

    def expand(match: re.Match) -> str:
        numbers = list(dict.fromkeys(int(n) for n in re.findall(r"\d+", match["numbers"])))
        numbers = [n for n in numbers if n in known]
        blocks = [f"[[实验图:figure-{n}]]" for n in numbers]
        if len(numbers) > 1:
            finding = re.search(r"(?m)^共同结论[：:][ \t]*(\S[^\n]*)$", match["fields"])
            if finding:
                members = ",".join(map(str, numbers))
                blocks.append(f"<!-- experiment-finding:{members} -->\n\n{finding[1].strip()}\n\n<!-- /experiment-finding -->")
        return "\n\n".join(blocks)

    return re.sub(
        r"(?m)^:::实验图组[ \t]+(?P<numbers>\d+(?:[ \t,，、]+\d+)*)[ \t]*\r?\n"
        r"(?P<fields>(?:(?!^:::|^#{1,6} )[^\n]*\n)*)^:::[ \t]*\r?$", expand, report,
    )


def figure_reading(figure: FigureCandidate) -> list[str]:
    parts = []
    if figure.explanation and figure.explanation != figure.caption and re.search(r"[一-鿿]", figure.explanation):
        if figure.explanation_basis == "text":
            parts.append("以下解读仅依据原文图注及正文。")
        parts.append(figure.explanation)
    else:
        parts.append("中文解读暂不可用。")
        if figure.caption:
            parts.append(f"<details><summary>原文图注</summary><p>{escape(figure.caption)}</p></details>")
    return parts


def experiment_block(figure: FigureCandidate, number: int, *, pdf_url: str = "paper.pdf") -> str:
    parts = [f"<!-- experiment-figure:{number} -->", f"#### Figure {number}"]
    if figure.image_available:
        parts.append(f"![论文实验图 Figure {number}]({figure.path.name})")
    else:
        parts.append("原图未能提取。")
    parts.extend(figure_reading(figure))
    source = (f"[查看原文第 {figure.page} 页]({pdf_url}#page={figure.page})" if figure.page > 0
              else f"[查看论文原文]({pdf_url})")
    parts.extend([source, "<!-- /experiment-figure -->"])
    return "\n\n".join(parts)


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
        # A bare label directly above its own slot is the renderer's heading.
        report = re.sub(
            rf"(?im)^#{{3,6}}[ \t]+(?:Figure|Fig\.?|图)[ \t]*{number}[ \t]*\n(?:[ \t]*\n)*(?={re.escape(slot)}[ \t]*$)",
            "", report,
        )
        rendered = experiment_block(figure, number)
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
