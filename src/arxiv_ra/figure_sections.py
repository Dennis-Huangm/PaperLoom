"""Section boundaries shared by HTML and PDF figure extraction."""
from __future__ import annotations

import re

EXPERIMENT_HEADING_RE = re.compile(
    r"^(?:(?:(?:\d+(?:\.\d+)*)[.\s]+)?(?:experiments?|experimental\s+(?:results?|setup|evaluation))|\d+(?:\.\d+)*[.\s]+(?:evaluation|results?))(?P<suffix>\s+[^.!?\n]{1,100})?\s*$",
    re.I,
)
END_BODY_RE = re.compile(
    r"^(?:(?:\d+(?:\.\d+)*|[A-Z])[.\s]+)?(?:appendi(?:x|ces)\b.*|references|bibliography|supplementary\s+.*)\s*$",
    re.I,
)
END_EXPERIMENT_RE = re.compile(
    r"^(?:\d+[.\s]+)?(?:conclusions?(?:\s+and\s+.*)?|limitations?|acknowledg(?:e)?ments?)\s*$",
    re.I,
)

# LaTeX often prints only the appendix letter and title, without “Appendix”.
LETTERED_APPENDIX_RE = re.compile(r"^[A-Z](?:\.\d+)*\.?\s+[A-Z][^.!?\n]{0,100}$")


def is_experiment_heading(heading: str, *, heading_style: bool = True) -> bool:
    match = EXPERIMENT_HEADING_RE.fullmatch(heading)
    if not match:
        return False
    # A descriptive unnumbered PDF line needs heading typography. Otherwise
    # labels inside plots could move subsequent method figures into results.
    return bool(heading_style or not match['suffix'] or re.match(r'^\d', heading))


def is_body_end(heading: str, *, allow_lettered: bool = True) -> bool:
    return bool(END_BODY_RE.match(heading) or (allow_lettered and LETTERED_APPENDIX_RE.match(heading)))
