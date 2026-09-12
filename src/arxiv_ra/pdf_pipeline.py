from __future__ import annotations

import re
from pathlib import Path

import pymupdf as fitz

from .config import PDFConfig
from .models import FigureCandidate, ParsedPaper
from .utils import normalize_space

CAPTION_RE = re.compile(r"^(?:figure|fig\.?|图)\s*\d+[.:：\s]", re.I)
FIGURE_CAPTION_RE = re.compile(r"^(?:figure|fig\.?)\s*(\d+)\s*[:.]", re.I)
EXPERIMENT_HEADING_RE = re.compile(
    r"^(?:(?:\d+(?:\.\d+)*)\s+)?(?:experiments?|experimental\s+(?:results?|setup))\s*$",
    re.I,
)
MAIN_FIGURE_TERMS = {
    "overview": 6,
    "framework": 6,
    "architecture": 6,
    "pipeline": 5,
    "overall": 4,
    "method": 3,
    "system": 3,
    "workflow": 4,
    "概览": 6,
    "框架": 6,
    "架构": 6,
    "流程": 5,
}


def score_figure(candidate: FigureCandidate) -> float:
    caption = candidate.caption.casefold()
    keyword_score = sum(weight for term, weight in MAIN_FIGURE_TERMS.items() if term in caption)
    early_page_score = max(0, 5 - candidate.page) * 0.6
    figure_one = 3 if re.search(r"(?:figure|fig\.?)\s*1(?:\D|$)", caption, re.I) else 0
    return round(keyword_score + early_page_score + figure_one, 3)


def is_reliable_main_figure(candidate: FigureCandidate) -> bool:
    """Reject small page fragments and figures without method/overview evidence."""
    caption = candidate.caption.casefold()
    semantic_signal = any(term in caption for term in MAIN_FIGURE_TERMS) or bool(
        re.search(r"(?:figure|fig\.?)\s*[12](?:\D|$)", caption, re.I)
    )
    if candidate.score < 6 or not semantic_signal:
        return False
    try:
        pixmap = fitz.Pixmap(str(candidate.path))
        width, height = pixmap.width, pixmap.height
    except (RuntimeError, ValueError):
        return False
    return width >= 640 and height >= 360 and width * height >= 500_000


def _overlaps_horizontally(rect: fitz.Rect, region: fitz.Rect) -> bool:
    return min(rect.x1, region.x1) - max(rect.x0, region.x0) > 4


def _experiment_start_page(page_texts: list[str], max_pages: int) -> int:
    for page_index, text in enumerate(page_texts[:max_pages]):
        if any(EXPERIMENT_HEADING_RE.match(normalize_space(line)) for line in text.splitlines()):
            return page_index
    return max_pages


def _figure_caption_blocks(page: fitz.Page) -> list[tuple[int, str, fitz.Rect]]:
    captions: list[tuple[int, str, fitz.Rect]] = []
    for block in page.get_text("dict").get("blocks", []):
        lines = block.get("lines") or []
        if not lines:
            continue
        line_texts = [
            "".join(span.get("text", "") for span in line.get("spans", []))
            for line in lines
        ]
        first_text = normalize_space(line_texts[0])
        match = FIGURE_CAPTION_RE.match(first_text)
        if not match:
            continue
        accepted: list[str] = []
        previous_bottom: float | None = None
        accepted_rects: list[fitz.Rect] = []
        for line, line_text in zip(lines, line_texts):
            line_rect = fitz.Rect(line["bbox"])
            if previous_bottom is not None and line_rect.y0 - previous_bottom > 2.2:
                break
            accepted.append(line_text)
            accepted_rects.append(line_rect)
            previous_bottom = line_rect.y1
        caption = normalize_space(" ".join(accepted))
        caption = caption.replace("�C", "–").replace("�", '"')
        caption = re.sub(r"(?<=\w)-\s+(?=[a-z])", "", caption)
        caption = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", caption)
        caption_rect = accepted_rects[0]
        for line_rect in accepted_rects[1:]:
            caption_rect |= line_rect
        captions.append((int(match.group(1)), caption, caption_rect))
    return captions


def _extract_pre_experiment_figures(
    document: fitz.Document,
    output_dir: Path,
    page_texts: list[str],
    max_pages: int,
) -> list[FigureCandidate]:
    figures_dir = output_dir / "figures"
    figures_dir.mkdir(parents=True, exist_ok=True)
    cutoff = _experiment_start_page(page_texts, max_pages)
    extracted: list[FigureCandidate] = []
    seen_numbers: set[int] = set()
    for page_index in range(min(cutoff, max_pages)):
        page = document[page_index]
        for figure_number, caption, caption_rect in _figure_caption_blocks(page):
            if figure_number in seen_numbers:
                continue
            horizontal_region = fitz.Rect(
                max(page.rect.x0, caption_rect.x0 - 3),
                page.rect.y0,
                min(page.rect.x1, caption_rect.x1 + 3),
                caption_rect.y0,
            )
            visual_rects: list[fitz.Rect] = []
            for info in page.get_images(full=True):
                for image_rect in page.get_image_rects(int(info[0])):
                    if (
                        image_rect.y0 >= 50
                        and image_rect.y1 <= caption_rect.y0 + 4
                        and _overlaps_horizontally(image_rect, horizontal_region)
                    ):
                        visual_rects.append(image_rect)
            for drawing in page.get_drawings():
                drawing_rect = drawing.get("rect")
                if (
                    drawing_rect
                    and drawing_rect.y0 >= 50
                    and drawing_rect.y1 <= caption_rect.y0 + 4
                    and _overlaps_horizontally(drawing_rect, horizontal_region)
                ):
                    visual_rects.append(drawing_rect)
            if visual_rects:
                top = max(50, min(rect.y0 for rect in visual_rects) - 5)
            else:
                top = max(50, caption_rect.y0 - page.rect.height * 0.45)
            bottom = caption_rect.y0 - 4
            if bottom - top < 70:
                continue
            clip = fitz.Rect(horizontal_region.x0, top, horizontal_region.x1, bottom)
            pixmap = page.get_pixmap(matrix=fitz.Matrix(2.2, 2.2), clip=clip, alpha=False)
            path = figures_dir / f"page-figure-{figure_number:02d}-page-{page_index + 1:02d}.png"
            pixmap.save(path)
            candidate = FigureCandidate(
                path=path,
                page=page_index + 1,
                caption=caption,
                kind="page_figure",
            )
            candidate.score = score_figure(candidate) + 5
            extracted.append(candidate)
            seen_numbers.add(figure_number)
    return extracted


def extract_pre_experiment_figures(
    pdf_path: Path,
    output_dir: Path,
    max_pages: int = 60,
) -> list[FigureCandidate]:
    document = fitz.open(pdf_path)
    try:
        page_count = min(len(document), max_pages)
        page_texts = [document[index].get_text("text") for index in range(page_count)]
        return _extract_pre_experiment_figures(document, output_dir, page_texts, page_count)
    finally:
        document.close()


class PDFParser:
    def __init__(self, config: PDFConfig) -> None:
        self.config = config

    def parse(self, pdf_path: Path, output_dir: Path) -> ParsedPaper:
        if self.config.use_docling_if_available:
            try:
                parsed = self._parse_docling(pdf_path, output_dir)
                parsed.figures = extract_pre_experiment_figures(
                    pdf_path, output_dir, self.config.max_pages
                ) + parsed.figures
                return parsed
            except (ImportError, ModuleNotFoundError):
                pass
            except Exception as exc:
                (output_dir / "docling-error.txt").write_text(str(exc), encoding="utf-8")
        return self._parse_pymupdf(pdf_path, output_dir)

    def _parse_docling(self, pdf_path: Path, output_dir: Path) -> ParsedPaper:
        from docling.datamodel.base_models import InputFormat
        from docling.datamodel.pipeline_options import PdfPipelineOptions
        from docling.document_converter import DocumentConverter, PdfFormatOption
        from docling_core.types.doc import PictureItem

        options = PdfPipelineOptions()
        options.generate_picture_images = True
        options.generate_page_images = True
        converter = DocumentConverter(format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=options)})
        result = converter.convert(pdf_path, max_num_pages=self.config.max_pages)
        figures_dir = output_dir / "figures"
        figures_dir.mkdir(parents=True, exist_ok=True)
        figures: list[FigureCandidate] = []
        index = 0
        for element, _level in result.document.iterate_items():
            if not isinstance(element, PictureItem):
                continue
            index += 1
            path = figures_dir / f"figure-{index:02d}.png"
            image = element.get_image(result.document)
            image.save(path, "PNG")
            try:
                caption = normalize_space(element.caption_text(result.document))
            except Exception:
                caption = ""
            page = 1
            if getattr(element, "prov", None):
                page = int(element.prov[0].page_no)
            candidate = FigureCandidate(path=path, page=page, caption=caption, kind="docling")
            candidate.score = score_figure(candidate)
            figures.append(candidate)
        markdown = result.document.export_to_markdown()
        return ParsedPaper(
            text=markdown,
            page_texts=[markdown],
            figures=sorted(figures, key=lambda item: item.score, reverse=True),
            parser="docling",
        )

    def _parse_pymupdf(self, pdf_path: Path, output_dir: Path) -> ParsedPaper:
        document = fitz.open(pdf_path)
        page_texts: list[str] = []
        figures: list[FigureCandidate] = []
        figures_dir = output_dir / "figures"
        figures_dir.mkdir(parents=True, exist_ok=True)
        seen_xrefs: set[int] = set()
        max_pages = min(len(document), self.config.max_pages)
        for page_index in range(max_pages):
            page = document[page_index]
            text = page.get_text("text")
            page_texts.append(text)
            captions = [
                normalize_space(line)
                for line in text.splitlines()
                if CAPTION_RE.match(normalize_space(line))
            ]
            caption = " | ".join(captions[:4])
            for image_index, info in enumerate(page.get_images(full=True), start=1):
                xref = int(info[0])
                width, height = int(info[2]), int(info[3])
                if xref in seen_xrefs or width < 180 or height < 120:
                    continue
                seen_xrefs.add(xref)
                try:
                    pixmap = fitz.Pixmap(document, xref)
                    if pixmap.alpha or pixmap.colorspace is None or pixmap.colorspace.n > 3:
                        pixmap = fitz.Pixmap(fitz.csRGB, pixmap)
                    path = figures_dir / f"page-{page_index + 1:02d}-image-{image_index:02d}.png"
                    pixmap.save(path)
                    candidate = FigureCandidate(path=path, page=page_index + 1, caption=caption)
                    candidate.score = score_figure(candidate) + min(width * height / 1_000_000, 2)
                    figures.append(candidate)
                except RuntimeError:
                    continue
        page_figures = _extract_pre_experiment_figures(
            document, output_dir, page_texts, max_pages
        )
        document.close()
        return ParsedPaper(
            text="\n\n".join(f"[第 {i + 1} 页]\n{text}" for i, text in enumerate(page_texts)),
            page_texts=page_texts,
            figures=page_figures + sorted(figures, key=lambda item: item.score, reverse=True),
            parser="pymupdf",
        )
