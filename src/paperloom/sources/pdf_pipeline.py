from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory

import pymupdf as fitz

from ..config import PDFConfig
from ..papers.models import FigureCandidate, ParsedPaper
from ..runtime.jobs.task_runtime import task_checkpoint, task_progress, task_supports_cancellation, task_warning
from ..utils import normalize_space, read_json
from .figure_sections import (
    BACK_MATTER_HEADING_RE,
    END_EXPERIMENT_RE,
    FIGURE_CAPTION_RE,
    appendix_heading_id,
    is_experiment_heading,
)

CAPTION_RE = re.compile(r"^(?:figure|fig\.?|图)\s*\d+[.:：|\s]", re.I)
APPENDIX_REFERENCE_RE = re.compile(r"(?i:\bappendix)\s+([A-Z])\b")
FIGURE_REFERENCE_RE = re.compile(r"\b(?:figures?|figs?\.?)\s*(\d+(?:(?:,\s*|\s+and\s+)\d+)*)\b", re.I)
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


def _figure_caption_blocks(blocks: list[dict]) -> list[tuple[int, str, fitz.Rect]]:
    captions: list[tuple[int, str, fitz.Rect]] = []
    for block in blocks:
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
        # Inline icons/font changes can put the caption prefix and its
        # continuation in separate PDF text blocks. Wrapped lines can also
        # restart in a new block. Require either an adjacent inline fragment
        # or a tightly spaced line aligned with the caption's left edge.
        joined = {id(block)}
        while True:
            continuation = None
            for other in blocks:
                other_lines = other.get("lines") or []
                if id(other) in joined or not other_lines:
                    continue
                first = fitz.Rect(other_lines[0]["bbox"])
                inline = any(
                    abs(first.y1 - rect.y1) <= 2.2
                    and -1 <= first.x0 - rect.x1 <= max(18, rect.height * 2)
                    for rect in accepted_rects
                )
                wrapped = (
                    abs(first.x0 - accepted_rects[0].x0) <= 3
                    and 2.2 < first.y1 - max(rect.y1 for rect in accepted_rects) <= first.height + 2.2
                    and abs(first.height - accepted_rects[0].height) <= 2.2
                )
                if inline or wrapped:
                    continuation = other
                    break
            if continuation is None:
                break
            joined.add(id(continuation))
            previous_bottom = None
            for line in continuation["lines"]:
                rect = fitz.Rect(line["bbox"])
                if previous_bottom is not None and rect.y0 - previous_bottom > 2.2:
                    break
                accepted.append("".join(span.get("text", "") for span in line.get("spans", [])))
                accepted_rects.append(rect)
                previous_bottom = rect.y1
        rows: list[list[tuple[fitz.Rect, str]]] = []
        for rect, text in sorted(zip(accepted_rects, accepted), key=lambda item: item[0].y1):
            if not rows or abs(rect.y1 - rows[-1][0][0].y1) > 2.2:
                rows.append([])
            rows[-1].append((rect, text))
        caption = normalize_space(" ".join(
            text for row in rows for _, text in sorted(row, key=lambda item: item[0].x0)
        ))
        caption = caption.replace("�C", "–").replace("�", '"')
        caption = re.sub(r"(?<=\w)-\s+(?=[a-z])", "", caption)
        caption = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", caption)
        caption_rect = accepted_rects[0]
        for line_rect in accepted_rects[1:]:
            caption_rect |= line_rect
        captions.append((int(match.group(1)), caption, caption_rect))
    return captions


@dataclass(frozen=True)
class _ExperimentReferences:
    appendices: set[str]
    figures: set[int]


def _extract_report_figures(
    document: fitz.Document,
    output_dir: Path,
    max_pages: int,
    only_number: int | None = None,
) -> list[FigureCandidate]:
    figures_dir = output_dir / "figures"
    figures_dir.mkdir(parents=True, exist_ok=True)
    role, section = "method", ""
    back_matter = False
    referenced_appendices: set[str] = set()
    referenced_figures: set[int] = set()
    extracted: list[FigureCandidate] = []
    seen_numbers: set[int] = set()
    for page_index in range(max_pages):
        task_checkpoint()
        page = document[page_index]
        events: list[tuple[fitz.Rect, str | _ExperimentReferences | tuple[int, str, fitz.Rect]]] = []
        text_blocks = page.get_text("dict", flags=fitz.TEXTFLAGS_DICT & ~fitz.TEXT_PRESERVE_IMAGES)["blocks"]
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
            lines = block.get("lines", [])
            texts = [normalize_space("".join(span["text"] for span in line["spans"])) for line in lines]
            split_heading = False
            # LaTeX can emit the appendix letter and title as separate lines on the same baseline.
            if len(lines) == 2 and re.fullmatch(r"[A-Z](?:\.\d+)*\.?", texts[0]):
                first, second = (fitz.Rect(line["bbox"]) for line in lines)
                if abs(first.y1 - second.y1) <= 2.2 and 0 <= second.x0 - first.x1 <= 32:
                    lines = [{"bbox": first | second, "spans": lines[0]["spans"] + lines[1]["spans"]}]
                    texts = [" ".join(texts)]
                    split_heading = True
            for line, text in zip(lines, texts):
                spans = line["spans"]
                heading_style = any(span.get("size", 0) > body_size * 1.1 or span.get("flags", 0) & 16 for span in spans)
                standalone = len(lines) == 1
                appendix = appendix_heading_id(text)
                if (is_experiment_heading(text, heading_style=heading_style)
                        or END_EXPERIMENT_RE.match(text)
                        or (standalone and (BACK_MATTER_HEADING_RE.fullmatch(text)
                                            or (appendix is not None and (heading_style or split_heading))))):
                    events.append((fitz.Rect(line["bbox"]), text))
            if texts and not FIGURE_CAPTION_RE.match(texts[0]):
                paragraph = " ".join(texts)
                appendices = set(APPENDIX_REFERENCE_RE.findall(paragraph))
                numbers = {int(number) for mention in FIGURE_REFERENCE_RE.findall(paragraph)
                           for number in re.findall(r"\d+", mention)}
                if appendices or numbers:
                    events.append((fitz.Rect(block["bbox"]), _ExperimentReferences(appendices, numbers)))
        events.extend((rect, (number, caption, rect))
                      for number, caption, rect in _figure_caption_blocks(text_blocks))
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
            if isinstance(value, _ExperimentReferences):
                if role == "experiment" and not back_matter:
                    referenced_appendices.update(value.appendices)
                    referenced_figures.update(value.figures)
                continue
            if isinstance(value, str):
                appendix = appendix_heading_id(value, allow_lettered=role != "method")
                if appendix is not None or BACK_MATTER_HEADING_RE.fullmatch(value):
                    if not referenced_appendices and referenced_figures <= seen_numbers:
                        return extracted
                    back_matter = True
                    role = "experiment" if appendix in referenced_appendices else "other"
                    section = value
                elif not back_matter and is_experiment_heading(value):
                    role = "experiment"
                    section = value
                elif not back_matter and END_EXPERIMENT_RE.match(value):
                    role = "other"
                continue
            figure_number, caption, caption_rect = value
            figure_role = "experiment" if back_matter and figure_number in referenced_figures else role
            if figure_role == "other" or figure_number in seen_numbers:
                previous_captions.append(caption_rect)
                continue
            if only_number is not None and figure_number != only_number:
                previous_captions.append(caption_rect)
                seen_numbers.add(figure_number)
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
                kind="page_figure", role=figure_role, section=section, image_available=available,
            )
            candidate.score = score_figure(candidate) + 5
            extracted.append(candidate)
            seen_numbers.add(figure_number)
            if only_number is not None:
                return extracted
    return extracted


def extract_report_figures(
    pdf_path: Path,
    output_dir: Path,
    max_pages: int = 60, *, only_number: int | None = None,
) -> list[FigureCandidate]:
    document = fitz.open(pdf_path)
    try:
        page_count = min(len(document), max_pages)
        return _extract_report_figures(document, output_dir, page_count, only_number)
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
        needs_docling, do_ocr = parser == "docling", True
        if parser != "docling":
            parsed = self._parse_pymupdf(pdf_path, output_dir)
            if self.config.use_docling_if_available:
                needs_docling, do_ocr = self._docling_requirements(pdf_path, parsed)
        if needs_docling:
            task_progress("正在使用 Docling 解析版面与识别扫描页…" if do_ocr
                          else "正在使用 Docling 解析表格版面…")
            try:
                convert = self._parse_docling_isolated if task_supports_cancellation() else self._parse_docling
                enhanced = convert(pdf_path, output_dir, do_ocr=do_ocr)
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
    def _docling_requirements(pdf_path: Path, parsed: ParsedPaper) -> tuple[bool, bool]:
        # A page number is not a usable text layer. Require a large raster too,
        # so blank pages or sparse text with a small logo don't start Docling.
        needs_layout = False
        with fitz.open(pdf_path) as document:
            for page, text in zip(document, parsed.page_texts):
                task_checkpoint()
                # Adjacent decimal cells can become e.g. "1656.5010.20".
                # Request layout extraction rather than guessing a split or
                # changing any value. This is a fallback signal, not an audit.
                if (re.search(r"(?im)^\s*(?:Table|表)\s*\d+", text)
                        and re.search(r"\d+\.\d{3,}\.\d+", text)):
                    needs_layout = True
                if len(text.strip()) >= 40:
                    continue
                for image in page.get_images(full=True):
                    if any((rect & page.rect).get_area() >= page.rect.get_area() / 2
                           for rect in page.get_image_rects(image[0])):
                        return True, True
        return needs_layout, False

    def _parse_docling_isolated(self, pdf_path: Path, output_dir: Path, *, do_ocr: bool) -> ParsedPaper:
        # Docling's converter can spend minutes inside native code. Stage its
        # outputs in a child process that cancellation can stop and reap.
        executable = sys.executable
        env = None
        if os.name == "nt" and sys.prefix != sys.base_prefix:
            # Bypass the Windows venv redirector so Popen owns the real process.
            executable = sys._base_executable
            env = dict(os.environ, __PYVENV_LAUNCHER__=sys.executable)
        with TemporaryDirectory(prefix=".docling-", dir=output_dir) as staging:
            stage = Path(staging).resolve()
            log_path = stage / "worker.log"
            command = [executable, "-m", "paperloom.sources.docling_worker",
                       str(pdf_path.resolve()), str(stage), str(self.config.max_pages),
                       str(int(do_ocr))]
            task_checkpoint()
            with log_path.open("wb") as log, subprocess.Popen(
                command, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, env=env,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            ) as process:
                try:
                    while process.poll() is None:
                        task_checkpoint()
                        try:
                            process.wait(timeout=0.2)
                        except subprocess.TimeoutExpired:
                            continue
                    task_checkpoint()
                finally:
                    if process.poll() is None:
                        process.kill()
                        process.wait()
            if process.returncode == 2:
                raise ImportError("Docling unavailable")
            if process.returncode:
                with log_path.open("rb") as log:
                    log.seek(max(0, log_path.stat().st_size - 4096))
                    detail = log.read().decode("utf-8", errors="replace").strip()
                raise RuntimeError(f"Docling 进程退出（{process.returncode}）\n{detail}")
            data = read_json(stage / "parsed.json")
            figures = [FigureCandidate(**{**figure, "path": output_dir / "figures" / figure["path"]})
                       for figure in data.pop("figures")]
            if figures and data["text"].strip():
                (output_dir / "figures").mkdir(exist_ok=True)
                for figure in figures:
                    shutil.move(stage / "figures" / figure.path.name, figure.path)
            return ParsedPaper(**data, figures=figures)

    def _parse_docling(self, pdf_path: Path, output_dir: Path, *, do_ocr: bool) -> ParsedPaper:
        from docling.datamodel.base_models import InputFormat
        from docling.datamodel.pipeline_options import PdfPipelineOptions
        from docling.document_converter import DocumentConverter, PdfFormatOption
        from docling_core.types.doc import PictureItem

        options = PdfPipelineOptions()
        options.do_ocr = do_ocr
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
                        if pixmap.colorspace is None:
                            continue  # A standalone mask has no color image to export.
                        # Indexed JPX images may report one channel while still
                        # requiring palette expansion before PNG encoding.
                        pixmap = fitz.Pixmap(fitz.csRGB, pixmap)
                        path = figures_dir / f"page-{page_index + 1:02d}-image-{image_index:02d}.png"
                        pixmap.save(path)
                        candidate = FigureCandidate(path=path, page=page_index + 1, caption=caption)
                        candidate.score = score_figure(candidate) + min(width * height / 1_000_000, 2)
                        figures.append(candidate)
                    except (RuntimeError, ValueError, fitz.mupdf.FzErrorArgument):
                        continue
            page_figures = _extract_report_figures(document, output_dir, max_pages)
            return ParsedPaper(
                text=("\n\n".join(f"[第 {i + 1} 页]\n{text}" for i, text in enumerate(page_texts))
                      if any(text.strip() for text in page_texts) else ""),
                page_texts=page_texts,
                figures=page_figures + sorted(figures, key=lambda item: item.score, reverse=True),
                parser="pymupdf",
                total_pages=total_pages,
            )
