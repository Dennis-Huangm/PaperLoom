from __future__ import annotations

import re
from collections import Counter
from pathlib import Path

import pymupdf as fitz

from .config import PDFConfig
from .models import FigureCandidate, ParsedPaper
from .task_runtime import task_checkpoint, task_progress, task_warning
from .utils import normalize_space
from .figure_sections import END_EXPERIMENT_RE, is_body_end, is_experiment_heading

CAPTION_RE = re.compile(r"^(?:figure|fig\.?|图)\s*\d+[.:：\s]", re.I)
FIGURE_CAPTION_RE = re.compile(r"^(?:figure|fig\.?)\s*(\d+)\s*[:.]", re.I)
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


def _extract_report_figures(
    document: fitz.Document,
    output_dir: Path,
    page_texts: list[str],
    max_pages: int,
) -> list[FigureCandidate]:
    figures_dir = output_dir / "figures"
    figures_dir.mkdir(parents=True, exist_ok=True)
    role, section = "method", ""
    extracted: list[FigureCandidate] = []
    seen_numbers: set[int] = set()
    for page_index in range(max_pages):
        page = document[page_index]
        headings = []
        text_blocks = page.get_text("dict").get("blocks", [])
        font_sizes: Counter[float] = Counter()
        for block in text_blocks:
            for line in block.get("lines", []):
                for span in line.get("spans", []):
                    font_sizes[round(span.get("size", 0), 1)] += len(span.get("text", ""))
        body_size = font_sizes.most_common(1)[0][0] if font_sizes else 11.0
        midpoint = page.rect.width / 2
        two_columns = (
            any(block.get("lines") and block["bbox"][2] < midpoint + 12 for block in text_blocks)
            and any(block.get("lines") and block["bbox"][0] > midpoint - 12 for block in text_blocks)
        )
        for block in text_blocks:
            for line in block.get("lines", []):
                text = normalize_space("".join(span.get("text", "") for span in line.get("spans", [])))
                spans = line.get("spans", [])
                heading_style = any(span.get("size", 0) > body_size * 1.1 or span.get("flags", 0) & 16 for span in spans)
                if (is_body_end(text, allow_lettered=heading_style)
                        or is_experiment_heading(text, heading_style=heading_style)
                        or END_EXPERIMENT_RE.match(text)):
                    headings.append((fitz.Rect(line["bbox"]), text))
        events: list[tuple[fitz.Rect, str | tuple[int, str, fitz.Rect]]] = list(headings)
        events.extend((rect, (number, caption, rect))
                      for number, caption, rect in _figure_caption_blocks(page))
        events.sort(key=lambda item: item[0].y0)
        if two_columns:
            ordered = []
            band = []
            for event in events:
                rect = event[0]
                if rect.x0 < midpoint - 12 and rect.x1 > midpoint + 12:
                    ordered.extend(sorted(band, key=lambda item: (item[0].x0 >= midpoint - 12, item[0].y0)))
                    ordered.append(event)
                    band = []
                else:
                    band.append(event)
            ordered.extend(sorted(band, key=lambda item: (item[0].x0 >= midpoint - 12, item[0].y0)))
            events = ordered
        previous_captions: list[fitz.Rect] = []
        for _, value in events:
            if isinstance(value, str):
                section = value
                if is_body_end(section, allow_lettered=role != "method"):
                    return extracted
                if is_experiment_heading(section):
                    role = "experiment"
                elif END_EXPERIMENT_RE.match(section):
                    role = "other"
                continue
            figure_number, caption, caption_rect = value
            if role == "other":
                continue
            if figure_number in seen_numbers:
                continue
            previous_caption_bottom = max(
                (rect.y1 + 4 for rect in previous_captions if _overlaps_horizontally(rect, caption_rect)),
                default=50.0,
            )
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
                        image_rect.y0 >= previous_caption_bottom
                        and image_rect.y1 <= caption_rect.y0 + 4
                        and _overlaps_horizontally(image_rect, horizontal_region)
                    ):
                        visual_rects.append(image_rect)
            for drawing in page.get_drawings():
                drawing_rect = drawing.get("rect")
                if (
                    drawing_rect
                    and drawing_rect.y0 >= previous_caption_bottom
                    and drawing_rect.y1 <= caption_rect.y0 + 4
                    and _overlaps_horizontally(drawing_rect, horizontal_region)
                ):
                    visual_rects.append(drawing_rect)
            # Keep the nearest vertical cluster. Earlier tables/figures in the
            # same column must not become part of this figure's illustration.
            cluster_top = caption_rect.y0
            for rect in sorted(visual_rects, key=lambda r: r.y1, reverse=True):
                if cluster_top - rect.y1 > 35:
                    break
                cluster_top = min(cluster_top, rect.y0)
            top = max(previous_caption_bottom, cluster_top - 5)
            bottom = caption_rect.y0 - 4
            previous_captions.append(caption_rect)
            available = cluster_top < caption_rect.y0 and bottom - top >= 70
            clip = fitz.Rect(horizontal_region.x0, top, horizontal_region.x1, bottom)
            path = figures_dir / f"page-figure-{figure_number:02d}-page-{page_index + 1:02d}.png"
            if available:
                page.get_pixmap(matrix=fitz.Matrix(2.2, 2.2), clip=clip, alpha=False).save(path)
            candidate = FigureCandidate(
                path=path,
                page=page_index + 1,
                caption=caption,
                kind="page_figure", role=role, section=section, image_available=available,
            )
            candidate.score = score_figure(candidate) + 5
            extracted.append(candidate)
            seen_numbers.add(figure_number)
    return extracted


def extract_report_figures(
    pdf_path: Path,
    output_dir: Path,
    max_pages: int = 60,
) -> list[FigureCandidate]:
    document = fitz.open(pdf_path)
    try:
        page_count = min(len(document), max_pages)
        page_texts = [document[index].get_text("text") for index in range(page_count)]
        return _extract_report_figures(document, output_dir, page_texts, page_count)
    finally:
        document.close()


def extract_pre_experiment_figures(pdf_path: Path, output_dir: Path, max_pages: int = 60) -> list[FigureCandidate]:
    """Compatibility entry point for callers requesting method figures only."""
    return [figure for figure in extract_report_figures(pdf_path, output_dir, max_pages)
            if figure.role == "method" and figure.image_available]


class PDFParser:
    def __init__(self, config: PDFConfig) -> None:
        self.config = config

    def parse(self, pdf_path: Path, output_dir: Path) -> ParsedPaper:
        task_checkpoint()
        parser = self.config.parser.strip().casefold()
        if parser not in {"pymupdf", "docling"}:
            raise ValueError("PDF 解析器必须是 pymupdf 或 docling")
        output_dir.mkdir(parents=True, exist_ok=True)
        parsed = None
        if parser != "docling":
            parsed = self._parse_pymupdf(pdf_path, output_dir)
        if parser == "docling" or (
            parsed is not None and self.config.use_docling_if_available
            and self._needs_docling(pdf_path, parsed)
        ):
            task_progress("正在使用 Docling 解析版面或识别扫描页…")
            try:
                enhanced = self._parse_docling(pdf_path, output_dir)
                if not enhanced.text.strip():
                    raise ValueError("Docling 未提取到有效正文")
                enhanced.figures = extract_report_figures(
                    pdf_path, output_dir, self.config.max_pages
                ) + enhanced.figures
                return enhanced
            except (ImportError, ModuleNotFoundError):
                task_warning("PDF 版面解析", "Docling 不可用，已保留 PDF 文本层；扫描内容可能不完整。")
            except Exception as exc:
                (output_dir / "docling-error.txt").write_text(str(exc), encoding="utf-8")
                task_warning("PDF 版面解析", "Docling 解析失败，已保留 PDF 文本层；扫描内容可能不完整。")
        return parsed if parsed is not None else self._parse_pymupdf(pdf_path, output_dir)

    @staticmethod
    def _needs_docling(pdf_path: Path, parsed: ParsedPaper) -> bool:
        # A page number is not a usable text layer. Require a large raster too,
        # so blank pages or sparse text with a small logo don't start Docling.
        with fitz.open(pdf_path) as document:
            for page, text in zip(document, parsed.page_texts):
                task_checkpoint()
                # Adjacent decimal cells can become e.g. "1656.5010.20".
                # Request layout extraction rather than guessing a split or
                # changing any value. This is a fallback signal, not an audit.
                if (re.search(r"(?im)^\s*(?:Table|表)\s*\d+", text)
                        and re.search(r"\d+\.\d{3,}\.\d+", text)):
                    return True
                if len(text.strip()) >= 40:
                    continue
                for image in page.get_images(full=True):
                    if any((rect & page.rect).get_area() >= page.rect.get_area() / 2
                           for rect in page.get_image_rects(image[0])):
                        return True
        return False

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
        # Docling Markdown has no stable physical page boundaries. Validate
        # quotes against the actual PDF, independently of the report parser.
        with fitz.open(pdf_path) as source:
            total_pages = len(source)
            page_texts = [source[i].get_text("text") for i in range(min(total_pages, self.config.max_pages))]
        return ParsedPaper(
            text=markdown,
            page_texts=page_texts,
            figures=sorted(figures, key=lambda item: item.score, reverse=True),
            parser="docling",
            total_pages=total_pages,
        )

    def _parse_pymupdf(self, pdf_path: Path, output_dir: Path) -> ParsedPaper:
        with fitz.open(pdf_path) as document:
            page_texts: list[str] = []
            figures: list[FigureCandidate] = []
            figures_dir = output_dir / "figures"
            figures_dir.mkdir(parents=True, exist_ok=True)
            seen_xrefs: set[int] = set()
            total_pages = len(document)
            max_pages = min(total_pages, self.config.max_pages)
            for page_index in range(max_pages):
                task_checkpoint()
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
            page_figures = _extract_report_figures(
                document, output_dir, page_texts, max_pages
            )
            return ParsedPaper(
                text=("\n\n".join(f"[第 {i + 1} 页]\n{text}" for i, text in enumerate(page_texts))
                      if any(text.strip() for text in page_texts) else ""),
                page_texts=page_texts,
                figures=page_figures + sorted(figures, key=lambda item: item.score, reverse=True),
                parser="pymupdf",
                total_pages=total_pages,
            )
