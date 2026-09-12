from __future__ import annotations

import argparse
import json
import os
import re
import shutil
from pathlib import Path

from arxiv_ra.arxiv_html import ArxivHtmlFigureClient
from arxiv_ra.config import load_config
from arxiv_ra.llm import LLMClient
from arxiv_ra.models import FigureCandidate
from arxiv_ra.pdf_pipeline import extract_pre_experiment_figures
from arxiv_ra.render import render_report
from arxiv_ra.report import finalize_report_structure
from arxiv_ra.utils import write_json


def _load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def _figure_number(figure: FigureCandidate) -> int:
    match = re.match(r"(?:Figure|Fig\.?)\s*(\d+)", figure.caption, re.I)
    return int(match.group(1)) if match else 10_000 + figure.page


def _stored_explanations(report_dir: Path) -> dict[int, str]:
    path = report_dir / "method-figures.json"
    if not path.exists():
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    explanations: dict[int, str] = {}
    for index, item in enumerate(payload, start=1):
        caption = str(item.get("caption") or "")
        match = re.match(r"(?:Figure|Fig\.?)\s*(\d+)", caption, re.I)
        number = int(match.group(1)) if match else index
        explanations[number] = str(item.get("explanation") or "")
    return explanations


def _method_figures(
    report_dir: Path,
    arxiv_id: str,
    html_client: ArxivHtmlFigureClient,
) -> list[FigureCandidate]:
    pdf_path = report_dir / "paper.pdf"
    pdf_figures = (
        extract_pre_experiment_figures(pdf_path, report_dir, max_pages=60)
        if pdf_path.exists()
        else []
    )
    try:
        html_figures = html_client.fetch(arxiv_id, report_dir) if arxiv_id else []
    except Exception:
        html_figures = []
    by_number = {_figure_number(figure): figure for figure in pdf_figures}
    for figure in html_figures:
        by_number[_figure_number(figure)] = figure
    extracted = [by_number[number] for number in sorted(by_number)][:8]
    stored = _stored_explanations(report_dir)
    method_figures: list[FigureCandidate] = []
    for index, figure in enumerate(extracted, start=1):
        destination = report_dir / f"method-figure-{index:02d}{figure.path.suffix}"
        if destination.resolve() != figure.path.resolve():
            shutil.copy2(figure.path, destination)
        figure.path = destination
        figure.explanation = stored.get(_figure_number(figure), "")
        method_figures.append(figure)
    return method_figures


def rebuild(
    project_root: Path,
    explain: bool = False,
    paper_filter: str = "",
) -> list[Path]:
    rebuilt: list[Path] = []
    html_client = ArxivHtmlFigureClient()
    llm = None
    if explain:
        _load_dotenv(project_root / ".env")
        llm = LLMClient(load_config(project_root / "config.yaml").llm)
    for report_path in sorted((project_root / "run").glob("????-??-??/reports/*/report.md")):
        metadata_path = report_path.parent / "metadata.json"
        title = report_path.parent.name
        arxiv_id = ""
        abstract = ""
        if metadata_path.exists():
            payload = json.loads(metadata_path.read_text(encoding="utf-8"))
            paper = payload.get("paper") or {}
            title = paper.get("title") or title
            arxiv_id = paper.get("arxiv_id") or ""
            abstract = paper.get("abstract") or ""
        if paper_filter and paper_filter not in arxiv_id:
            continue
        markdown_text = report_path.read_text(encoding="utf-8")
        figures = _method_figures(report_path.parent, arxiv_id, html_client)
        if llm:
            for figure in figures:
                try:
                    figure.explanation = llm.describe_figure(
                        figure.path, title, abstract, figure.caption
                    )
                except Exception:
                    figure.explanation = llm.chat(
                        "你是论文图示讲解助手，不得照抄或逐字翻译原始 caption。",
                        f"论文：{title}\n摘要：{abstract[:1200]}\n原始 caption：{figure.caption}\n\n请用 2-4 句通俗中文重新解释图的输入、步骤、输出和核心含义。",
                    ).strip()
        normalized = finalize_report_structure(markdown_text, figures)
        report_path.write_text(normalized, encoding="utf-8")
        render_report(normalized, report_path.with_suffix(".html"), title)
        write_json(
            report_path.parent / "method-figures.json",
            [
                {
                    "filename": figure.path.name,
                    "page": figure.page,
                    "caption": figure.caption,
                    "source": figure.kind,
                    "explanation": figure.explanation,
                }
                for figure in figures
            ],
        )
        rebuilt.append(report_path)
    return rebuilt


def main() -> None:
    parser = argparse.ArgumentParser(description="Normalize and rebuild all local paper reports.")
    parser.add_argument("project_root", nargs="?", default=".")
    parser.add_argument("--explain", action="store_true", help="Use the configured LLM to explain each method figure.")
    parser.add_argument("--paper", default="", help="Only rebuild an arXiv ID containing this value.")
    args = parser.parse_args()
    root = Path(args.project_root).resolve()
    for path in rebuild(root, explain=args.explain, paper_filter=args.paper):
        print(path)


if __name__ == "__main__":
    main()
