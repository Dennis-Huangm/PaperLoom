from __future__ import annotations

import mimetypes
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup, Tag

from .. import __version__
from ..papers.models import FigureCandidate
from ..utils import normalize_space
from .figure_sections import END_EXPERIMENT_RE, is_body_end, is_experiment_heading


FIGURE_NUMBER_RE = re.compile(r"^(?:Figure|Fig\.?)\s*(\d+)\s*[:.]", re.I)


@dataclass(slots=True)
class HtmlFigureSpec:
    number: int
    caption: str
    image_url: str
    role: str = "method"
    section: str = ""


def parse_html_figure_specs(html_text: str, base_url: str) -> list[HtmlFigureSpec]:
    soup = BeautifulSoup(html_text, "html.parser")
    specs: list[HtmlFigureSpec] = []
    seen: set[int] = set()
    role, section = "method", ""
    for node in soup.find_all(["h1", "h2", "h3", "h4", "h5", "h6", "figure"]):
        if node.find_parent(class_="ltx_appendix") is not None:
            break
        if node.name != "figure":
            if "ltx_title_document" in (node.get("class") or []):
                continue
            section = normalize_space(node.get_text(" ", strip=True))
            if is_body_end(section, allow_lettered=role != "method"):
                break
            if is_experiment_heading(section):
                role = "experiment"
            elif END_EXPERIMENT_RE.match(section):
                role = "other"
            continue
        if role == "other":
            continue
        if not isinstance(node, Tag) or "ltx_figure" not in (node.get("class") or []):
            continue
        if node.find_parent("figure", class_="ltx_figure") is not None:
            continue
        caption_node = node.find("figcaption", recursive=False) or node.find("figcaption")
        caption = normalize_space(caption_node.get_text(" ", strip=True) if caption_node else "")
        match = FIGURE_NUMBER_RE.match(caption)
        if not match:
            continue
        number = int(match.group(1))
        if number in seen:
            continue
        sources = []
        for image in node.find_all("img"):
            source = image.get("src")
            if isinstance(source, str) and source:
                absolute = urljoin(base_url, source)
                if absolute not in sources:
                    sources.append(absolute)
        # A single HTML image normally represents the complete LaTeX figure. If the
        # figure is split into multiple images, keep PDF layout reconstruction as fallback.
        specs.append(HtmlFigureSpec(number, caption, sources[0] if len(sources) == 1 else "", role, section))
        seen.add(number)
    return specs


class ArxivHtmlFigureClient:
    def __init__(self, timeout: float = 45.0) -> None:
        self.client = httpx.Client(
            timeout=timeout,
            follow_redirects=True,
            headers={"User-Agent": f"PaperLoom/{__version__} (personal research use)"},
        )

    def fetch(self, arxiv_id: str, output_dir: Path) -> list[FigureCandidate]:
        html_url = f"https://arxiv.org/html/{arxiv_id}"
        try:
            response = self.client.get(html_url)
        except httpx.HTTPError:
            return []
        if response.status_code != 200 or "text/html" not in response.headers.get("content-type", ""):
            return []
        specs = parse_html_figure_specs(response.text, str(response.url))
        figures_dir = output_dir / "figures"
        figures_dir.mkdir(parents=True, exist_ok=True)
        figures: list[FigureCandidate] = []
        for spec in specs:
            candidate = FigureCandidate(
                path=figures_dir / f"unavailable-figure-{spec.number:02d}.png",
                page=0, caption=spec.caption, kind="html_figure",
                role=spec.role, section=spec.section, image_available=False,
            )
            figures.append(candidate)
            if not spec.image_url:
                continue
            try:
                image_response = self.client.get(spec.image_url)
            except httpx.HTTPError:
                continue
            content_type = image_response.headers.get("content-type", "").split(";", 1)[0]
            if image_response.status_code != 200 or not content_type.startswith("image/"):
                continue
            if len(image_response.content) < 1024:
                continue
            suffix = Path(urlparse(spec.image_url).path).suffix
            if not suffix:
                suffix = mimetypes.guess_extension(content_type) or ".png"
            path = figures_dir / f"html-figure-{spec.number:02d}{suffix.lower()}"
            path.write_bytes(image_response.content)
            candidate.path = path
            candidate.score = 20 - spec.number * 0.01
            candidate.image_available = True
        return figures
