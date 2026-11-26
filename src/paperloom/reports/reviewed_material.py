"""Reuse locally reviewed material only for the paper and inputs it was checked against."""
from pathlib import Path

from ..runtime.jobs.task_runtime import task_warning
from ..utils import read_json
from .report_checkpoint import file_digest, fingerprint
from .report_figures import figure_number


def reviewed_material(root, paper):
    path = Path(root) / "metadata-cache" / "reviews" / (fingerprint([paper.arxiv_id, paper.version]) + ".json")
    try:
        record = read_json(path, {})
        if (record.get("arxiv_id") == paper.arxiv_id and record.get("version") == paper.version
                and record.get("title") == paper.title and record.get("authors") == [a.name for a in paper.authors]):
            return record
    except (OSError, ValueError, TypeError, AttributeError):
        task_warning("本地核对记录", "记录无法读取，已按当前论文材料重新处理。")
    return {}


def apply_publication_review(verified, review):
    """Retain cited local date precision only for compatible publication records."""
    if not isinstance(review, dict) or any("来源冲突" in note for note in verified.conflicts):
        return False
    venues, sources = review.get("venues"), review.get("sources")
    if not all(isinstance(items, list) and items and all(isinstance(s, str) and s.strip() for s in items)
               for items in (venues, sources)):
        return False
    doi = review.get("doi")
    if (doi is not None and not isinstance(doi, str)
            or verified.doi and doi and verified.doi.casefold() != doi.casefold()
            or verified.venue and verified.venue not in venues):
        return False
    date = verified.publication_date
    reviewed_date = review.get("date")
    if reviewed_date is not None and not isinstance(reviewed_date, str):
        return False
    if date and reviewed_date and not (date.startswith(reviewed_date) or reviewed_date.startswith(date)):
        return False
    if reviewed_date and (not date or reviewed_date.startswith(date)):
        verified.publication_date = reviewed_date
    verified.venue = venues[0]
    verified.venue_status = "verified_metadata"
    verified.doi = verified.doi or doi
    verified.sources = list(dict.fromkeys([*verified.sources, *sources]))
    return True


def figure_review_signature(figure, explanation=None):
    text = figure.explanation if explanation is None else explanation
    return fingerprint([file_digest(figure.path), figure.caption, figure.page, text])


def apply_figure_review(figure, readings):
    reading = readings.get(str(figure_number(figure))) if isinstance(readings, dict) else None
    if not isinstance(reading, dict) or not figure.image_available:
        return
    text = reading.get("explanation")
    if isinstance(text, str) and text.strip() and reading.get("signature") == figure_review_signature(figure, text):
        figure.explanation = text
        figure.explanation_basis = "visual"
        figure.review_signature = reading["signature"]
