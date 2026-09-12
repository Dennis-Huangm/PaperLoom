from pathlib import Path

import fitz

from arxiv_ra.models import FigureCandidate
from arxiv_ra.pdf_pipeline import (
    extract_pre_experiment_figures,
    is_reliable_main_figure,
    score_figure,
)


def test_framework_figure_beats_late_result_plot() -> None:
    framework = FigureCandidate(Path("a.png"), 2, "Figure 1: Overview of our framework")
    result = FigureCandidate(Path("b.png"), 9, "Figure 8: Accuracy results")
    assert score_figure(framework) > score_figure(result)


def _render_test_image(path: Path, width: int, height: int) -> None:
    document = fitz.open()
    page = document.new_page(width=width, height=height)
    page.draw_rect(page.rect, color=(0, 0, 0), fill=(1, 1, 1))
    page.get_pixmap().save(path)
    document.close()


def test_reliable_main_figure_rejects_small_page_fragment(tmp_path: Path) -> None:
    path = tmp_path / "small.png"
    _render_test_image(path, 192, 191)
    candidate = FigureCandidate(path, 2, "Figure 1: Overview of our framework")
    candidate.score = score_figure(candidate)

    assert not is_reliable_main_figure(candidate)


def test_reliable_main_figure_accepts_large_overview(tmp_path: Path) -> None:
    path = tmp_path / "overview.png"
    _render_test_image(path, 900, 600)
    candidate = FigureCandidate(path, 2, "Figure 1: Overview of our framework")
    candidate.score = score_figure(candidate)

    assert is_reliable_main_figure(candidate)


def test_extracts_complete_figures_before_experiments_only(tmp_path: Path) -> None:
    pdf_path = tmp_path / "paper.pdf"
    document = fitz.open()
    for number in (1, 2):
        page = document.new_page(width=612, height=792)
        page.draw_rect(fitz.Rect(108, 72, 504, 185), color=(0, 0, 0), fill=(0.9, 0.9, 0.9))
        page.insert_text((108, 210), f"Figure {number}: Overview of method stage {number}.", fontsize=10)
    experiment_page = document.new_page(width=612, height=792)
    experiment_page.insert_text((108, 90), "4 Experiments", fontsize=14)
    experiment_page.draw_rect(fitz.Rect(108, 120, 504, 230), color=(0, 0, 0))
    experiment_page.insert_text((108, 250), "Figure 3: Experimental results.", fontsize=10)
    document.save(pdf_path)
    document.close()

    figures = extract_pre_experiment_figures(pdf_path, tmp_path / "output")

    assert [figure.page for figure in figures] == [1, 2]
    assert all(figure.kind == "page_figure" for figure in figures)
    assert all(figure.path.exists() for figure in figures)
