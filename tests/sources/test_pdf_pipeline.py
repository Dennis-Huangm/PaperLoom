from pathlib import Path
from types import SimpleNamespace

import pytest

import fitz

from paperloom.config import PDFConfig
from paperloom.papers.models import FigureCandidate
from paperloom.sources.pdf_pipeline import PDFParser, extract_report_figures, extract_pre_experiment_figures, is_reliable_main_figure, score_figure


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


@pytest.mark.parametrize(('heading', 'fontsize', 'expected'), [
    ('4 Experiments on ImageNet', 14, 'experiment'),
    ('4 Results and Analysis', 14, 'experiment'),
    ('Experiments on ImageNet', 14, 'experiment'),
    ('Experiments on ImageNet', 10, 'method'),
])
def test_descriptive_experiment_headings_require_structure(tmp_path, heading, fontsize, expected):
    pdf = tmp_path / 'descriptive-heading.pdf'
    with fitz.open() as document:
        page = document.new_page()
        page.insert_text((70, 80), heading, fontsize=fontsize)
        page.draw_rect(fitz.Rect(70, 120, 400, 260))
        page.insert_text((70, 285), 'Figure 9: Benchmark results and comparison.', fontsize=10)
        document.save(pdf)
    assert [figure.role for figure in extract_report_figures(pdf, tmp_path / 'out')] == [expected]


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


def _text_pdf(path):
    with fitz.open() as document:
        page = document.new_page()
        page.insert_text((40, 60), 'A readable method with input, intermediate states and output. ' * 2, fontsize=8)
        document.save(path)


def _install_docling_adapter(monkeypatch, convert):
    """Replace the optional third-party converter, not the parser under test."""
    import sys

    class Converter:
        def __init__(self, **options):
            pass

        def convert(self, path, **options):
            return convert(path, **options)

    monkeypatch.setitem(sys.modules, 'docling.datamodel.base_models', SimpleNamespace(
        InputFormat=SimpleNamespace(PDF='pdf')))
    monkeypatch.setitem(sys.modules, 'docling.datamodel.pipeline_options', SimpleNamespace(
        PdfPipelineOptions=SimpleNamespace))
    monkeypatch.setitem(sys.modules, 'docling.document_converter', SimpleNamespace(
        DocumentConverter=Converter, PdfFormatOption=lambda **kwargs: kwargs))
    monkeypatch.setitem(sys.modules, 'docling_core.types.doc', SimpleNamespace(PictureItem=type('PictureItem', (), {})))


def test_text_pdf_uses_selected_fast_parser_without_starting_docling(tmp_path, monkeypatch):
    calls = []

    def convert(path, **options):
        calls.append(path)
        return SimpleNamespace(document=SimpleNamespace(
            export_to_markdown=lambda: 'Unnecessary heavy conversion', iterate_items=lambda: []))

    _install_docling_adapter(monkeypatch, convert)
    pdf = tmp_path / 'text.pdf'
    _text_pdf(pdf)
    parsed = PDFParser(PDFConfig(parser='pymupdf', use_docling_if_available=True)).parse(pdf, tmp_path / 'out')
    assert parsed.parser == 'pymupdf'
    assert 'A readable method' in parsed.text
    assert parsed.total_pages == len(parsed.page_texts) == 1
    assert calls == []


def test_mixed_scanned_pdf_can_use_docling_but_blank_pages_do_not_trigger_it(tmp_path, monkeypatch):
    calls = []

    def convert(path, **options):
        calls.append(path)
        return SimpleNamespace(document=SimpleNamespace(
            export_to_markdown=lambda: 'Recognized scanned method and its text page.', iterate_items=lambda: []))

    _install_docling_adapter(monkeypatch, convert)
    scan = fitz.open()
    page = scan.new_page()
    page.insert_text((40, 60), 'Scanned method content')
    image = page.get_pixmap().tobytes('png')
    scan.close()
    pdf = tmp_path / 'mixed.pdf'
    with fitz.open() as document:
        document.new_page().insert_text((40, 60), 'A readable method explains the entire sequence of operations.' * 2, fontsize=8)
        page = document.new_page()
        page.insert_image(page.rect, stream=image)
        page.insert_text((40, 780), '2')
        document.save(pdf)
    parsed = PDFParser(PDFConfig()).parse(pdf, tmp_path / 'mixed-out')
    assert parsed.parser == 'docling'
    assert 'Recognized scanned method' in parsed.text
    assert parsed.total_pages == len(parsed.page_texts) == 2
    assert calls == [pdf]

    calls.clear()
    blank = tmp_path / 'blank.pdf'
    with fitz.open() as document:
        document.new_page()
        document.save(blank)
    parsed = PDFParser(PDFConfig()).parse(blank, tmp_path / 'blank-out')
    assert parsed.parser == 'pymupdf'
    assert calls == []


def test_tightly_packed_table_keeps_structured_parser_fallback(tmp_path, monkeypatch):
    calls = []

    def convert(path, **options):
        calls.append(path)
        return SimpleNamespace(document=SimpleNamespace(
            export_to_markdown=lambda: '| Method | rMSE | MSE |\n|---|---|---|\n| Model A | 1656.50 | 10.20 |',
            iterate_items=lambda: []))

    _install_docling_adapter(monkeypatch, convert)
    pdf = tmp_path / 'packed-table.pdf'
    with fitz.open() as document:
        page = document.new_page()
        page.insert_text((40, 60), 'Table 3: Editing results.')
        page.insert_text((40, 90), 'Model A    1656.5010.20')
        document.save(pdf)
    parsed = PDFParser(PDFConfig()).parse(pdf, tmp_path / 'out')
    assert calls == [pdf]
    assert parsed.parser == 'docling'
    assert '| 1656.50 | 10.20 |' in parsed.text


def test_empty_structured_result_keeps_readable_pdf_text(tmp_path, monkeypatch):
    _install_docling_adapter(monkeypatch, lambda *a, **k: SimpleNamespace(document=SimpleNamespace(
        export_to_markdown=lambda: '', iterate_items=lambda: [])))
    pdf = tmp_path / 'text.pdf'
    _text_pdf(pdf)
    parsed = PDFParser(PDFConfig(parser='docling', use_docling_if_available=False)).parse(pdf, tmp_path / 'out')
    assert parsed.parser == 'pymupdf'
    assert 'A readable method' in parsed.text


def test_image_only_pdf_without_ocr_is_not_reported_as_readable_text(tmp_path):
    image = tmp_path / 'scan.png'
    _render_test_image(image, 400, 600)
    pdf = tmp_path / 'scan.pdf'
    with fitz.open() as document:
        page = document.new_page()
        page.insert_image(page.rect, filename=str(image))
        document.save(pdf)
    parsed = PDFParser(PDFConfig(use_docling_if_available=False)).parse(pdf, tmp_path / 'out')
    assert not parsed.text.strip()
    assert parsed.page_texts == ['']
    assert parsed.total_pages == 1


def test_unknown_parser_is_rejected_instead_of_silently_using_another(tmp_path):
    pdf = tmp_path / 'text.pdf'
    _text_pdf(pdf)
    with pytest.raises(ValueError, match='解析器'):
        PDFParser(PDFConfig(parser='unknown')).parse(pdf, tmp_path / 'out')


def test_cancellation_during_docling_propagates_without_text_fallback(tmp_path, monkeypatch):
    from paperloom.runtime.jobs.task_runtime import TaskCancelled

    def cancel(*args, **kwargs):
        raise TaskCancelled()

    _install_docling_adapter(monkeypatch, cancel)
    pdf = tmp_path / 'text.pdf'
    _text_pdf(pdf)
    with pytest.raises(TaskCancelled):
        PDFParser(PDFConfig(parser='docling')).parse(pdf, tmp_path / 'out')


def test_docling_failure_keeps_text_and_reports_its_limitations(tmp_path, monkeypatch):
    from paperloom.runtime.jobs.task_runtime import TaskHooks, bind_task_hooks

    def fail(*args, **kwargs):
        raise RuntimeError('Offline converter failure')

    _install_docling_adapter(monkeypatch, fail)
    pdf = tmp_path / 'text.pdf'
    _text_pdf(pdf)
    warnings = []
    hooks = TaskHooks(progress=lambda *a: None, warning=lambda *args: warnings.append(args), is_cancelled=lambda: False)
    with bind_task_hooks(hooks):
        parsed = PDFParser(PDFConfig(parser='docling')).parse(pdf, tmp_path / 'out')
    assert parsed.parser == 'pymupdf' and 'A readable method' in parsed.text
    assert warnings and 'Docling' in warnings[0][1]
    assert 'Offline converter failure' in (tmp_path / 'out/docling-error.txt').read_text(encoding='utf-8')
