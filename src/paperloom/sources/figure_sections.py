"""Section boundaries shared by HTML and PDF figure extraction."""
from __future__ import annotations

import re

FIGURE_CAPTION_RE = re.compile(r"^(?:figure|fig\.?)\s*(\d+)\s*[:.|]", re.I)

EXPERIMENT_HEADING_RE = re.compile(
    r"^(?:(?:(?:\d+(?:\.\d+)*)[.\s]+)?(?:experiments?|experimental\s+(?:results?|setup|evaluation))|\d+(?:\.\d+)*[.\s]+(?:evaluation|results?|empirical\s+analysis))(?P<suffix>\s+[^.!?\n]{1,100})?\s*$",
    re.I,
)
BACK_MATTER_HEADING_RE = re.compile(
    r"^(?:(?:\d+(?:\.\d+)*|[A-Z])[.\s]+)?(?:references|bibliography|supplementary\s+.*)$",
    re.I,
)
APPENDIX_HEADING_RE = re.compile(
    r"^(?:\d+(?:\.\d+)*[.\s]+)?(?i:appendix)(?:\s+([A-Z])(?:\.\d+)*"
    r"(?:\s*[:.–-]\s*\w[^!?]*|\s+[A-Z][^.!?]*)?|\s*[:–-]\s*\w[^!?]*)?$|^(?i:appendices)$",
)
END_EXPERIMENT_RE = re.compile(
    r"^(?:\d+[.\s]+)?(?:conclusions?(?:\s+and\s+.*)?|limitations?|acknowledg(?:e)?ments?)\s*$",
    re.I,
)

# LaTeX often prints only the appendix letter and title, without “Appendix”.
LETTERED_APPENDIX_RE = re.compile(r"^([A-Z])(?:\.\d+)*\.?\s+[A-Z][^.!?\n]{0,100}$")


def appendix_heading_id(heading: str, *, allow_lettered: bool = True) -> str | None:
    """Return the appendix letter, or an empty string for an unlettered heading."""
    match = APPENDIX_HEADING_RE.fullmatch(heading)
    if match:
        return match[1] or ""
    if allow_lettered and (match := LETTERED_APPENDIX_RE.fullmatch(heading)):
        return match[1]
    return None


def is_experiment_heading(heading: str, *, heading_style: bool = True) -> bool:
    match = EXPERIMENT_HEADING_RE.fullmatch(heading)
    if not match:
        return False
    # A descriptive unnumbered PDF line needs heading typography. Otherwise
    # labels inside plots could move subsequent method figures into results.
    return bool(heading_style or not match['suffix'] or re.match(r'^\d', heading))


def is_body_end(heading: str, *, allow_lettered: bool = True) -> bool:
    return bool(BACK_MATTER_HEADING_RE.fullmatch(heading) or
                appendix_heading_id(heading, allow_lettered=allow_lettered) is not None)
