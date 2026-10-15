from pathlib import Path

import fitz

from arxiv_ra.models import FigureCandidate
from arxiv_ra.pdf_pipeline import (
    extract_report_figures,
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


def test_report_figures_include_experiment_on_boundary_page_but_not_appendix(tmp_path: Path) -> None:
    pdf = tmp_path / "mixed.pdf"
    with fitz.open() as doc:
        page = doc.new_page()
        page.draw_rect(fitz.Rect(70, 65, 400, 160))
        page.insert_text((70, 180), "Figure 1: Method overview.")
        page.insert_text((70, 260), "3 Experiments")
        page.draw_rect(fitz.Rect(70, 300, 400, 420))
        page.insert_text((70, 440), "Figure 5: Revision budget results.")
        page = doc.new_page()
        page.insert_text((70, 65), "A Additional Results", fontsize=14)
        page.draw_rect(fitz.Rect(70, 100, 400, 200))
        page.insert_text((70, 230), "Figure 6: Supplementary results.")
        doc.save(pdf)
    figures = extract_report_figures(pdf, tmp_path / "out")
    assert [(f.caption, f.role) for f in figures] == [
        ("Figure 1: Method overview.", "method"),
        ("Figure 5: Revision budget results.", "experiment"),
    ]
    assert all(f.path.is_file() for f in figures)


def test_plot_labels_do_not_start_experiments_and_columns_keep_complete_figures(tmp_path: Path) -> None:
    pdf = tmp_path / "columns.pdf"
    with fitz.open() as doc:
        page = doc.new_page()
        page.insert_text((70, 100), "Result")
        page.draw_rect(fitz.Rect(70, 120, 260, 220))
        page.insert_text((70, 245), "Figure 1: Overview.")
        page.draw_rect(fitz.Rect(320, 120, 520, 280))
        page.insert_text((320, 305), "Figure 2: Method details.")
        doc.save(pdf)
    figures = extract_report_figures(pdf, tmp_path / "out")
    assert [f.role for f in figures] == ["method", "method"]
    assert all(f.image_available for f in figures)


def test_pdf_experiment_boundary_follows_columns_before_vertical_position(tmp_path: Path) -> None:
    pdf = tmp_path / "column-transition.pdf"
    with fitz.open() as doc:
        page = doc.new_page()
        page.insert_text((70, 400), "4 Experiments")
        page.draw_rect(fitz.Rect(320, 80, 520, 210))
        page.insert_text((320, 235), "Figure 5: Experiment results.")
        doc.save(pdf)
    figures = extract_report_figures(pdf, tmp_path / "out")
    assert [(f.role, f.image_available) for f in figures] == [("experiment", True)]


def test_pdf_title_and_plot_label_are_not_lettered_appendix_headings(tmp_path: Path) -> None:
    pdf = tmp_path / "title-label.pdf"
    with fitz.open() as doc:
        page = doc.new_page()
        page.insert_text((70, 65), "A Unified Framework for Editing", fontsize=18)
        page.insert_text((70, 100), "3 Experiments", fontsize=14)
        page.draw_rect(fitz.Rect(70, 130, 400, 270))
        page.insert_text((90, 180), "A Strong Baseline")
        page.insert_text((70, 295), "Figure 5: Results on benchmark A.")
        doc.save(pdf)
    assert [f.role for f in extract_report_figures(pdf, tmp_path / "out")] == ["experiment"]
