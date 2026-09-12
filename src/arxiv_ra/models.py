from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any


@dataclass(slots=True)
class Author:
    name: str
    affiliations: list[str] = field(default_factory=list)


@dataclass(slots=True)
class Paper:
    arxiv_id: str
    title: str
    authors: list[Author]
    abstract: str
    categories: list[str]
    primary_category: str
    published: datetime
    updated: datetime
    abs_url: str
    pdf_url: str
    version: int = 1
    doi: str | None = None
    journal_ref: str | None = None
    comment: str | None = None
    lexical_score: float = 0.0
    llm_score: float | None = None
    feedback_score: float = 0.0
    recommendation_reason: str = ""

    @property
    def final_score(self) -> float:
        if self.llm_score is None:
            return self.lexical_score + self.feedback_score
        normalized_lexical = max(0.0, min(10.0, self.lexical_score))
        return 0.35 * normalized_lexical + 0.65 * self.llm_score + self.feedback_score

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["published"] = self.published.isoformat()
        value["updated"] = self.updated.isoformat()
        value["final_score"] = self.final_score
        return value


@dataclass(slots=True)
class VerifiedMetadata:
    title: str = ""
    authors: list[Author] = field(default_factory=list)
    venue: str | None = None
    venue_status: str = "unverified"
    publication_date: str | None = None
    doi: str | None = None
    citation_count: int | None = None
    sources: list[str] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class FigureCandidate:
    path: Path
    page: int
    caption: str
    score: float = 0.0
    kind: str = "embedded"
    explanation: str = ""


@dataclass(slots=True)
class ParsedPaper:
    text: str
    page_texts: list[str]
    figures: list[FigureCandidate] = field(default_factory=list)
    parser: str = "pymupdf"


@dataclass(slots=True)
class ReportArtifact:
    paper: Paper
    report_path: Path
    main_figure: FigureCandidate | None
    metadata: VerifiedMetadata
    report_markdown: str
