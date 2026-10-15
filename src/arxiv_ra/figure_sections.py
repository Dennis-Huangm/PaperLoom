"""Section boundaries shared by HTML and PDF figure extraction."""
from __future__ import annotations

import re

EXPERIMENT_HEADING_RE = re.compile(
    r"^(?:(?:(?:\d+(?:\.\d+)*)[.\s]+)?(?:experiments?|experimental\s+(?:results?|setup|evaluation))|\d+(?:\.\d+)*[.\s]+(?:evaluation|results?(?:\s+and\s+discussion)?))\s*$",
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


def is_body_end(heading: str) -> bool:
    return bool(END_BODY_RE.match(heading) or LETTERED_APPENDIX_RE.match(heading))
