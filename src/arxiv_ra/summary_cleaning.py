from __future__ import annotations

import re


_PDF_REFERENCE = re.compile(
    r"\s*(?:[、，,;；]\s*)?\[?原文\s*\d+\s*·\s*PDF\s*第\s*\d+\s*页"
    r"\]?(?:\([^)]*\))?"
)


def clean_summary_text(value: str) -> str:
    """Remove report-only evidence markers from short recommendation prose."""
    text = _PDF_REFERENCE.sub("", value or "")
    text = re.sub(r"(?:^|\s)---\s*$", "", text)
    text = re.sub(r"([。！？.!?])\s*[。；;、，,]+", r"\1", text)
    text = re.sub(r"\s+([。！？.!?])", r"\1", text)
    return text.strip()
