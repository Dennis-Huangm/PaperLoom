from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
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

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "Paper":
        """Rehydrate a paper snapshot saved by recommendations or the library."""
        def parse_date(raw: Any) -> datetime:
            try:
                parsed = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
            except (TypeError, ValueError):
                parsed = datetime.now(timezone.utc)
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)

        authors = [
            Author(
                name=str(item.get("name") or ""),
                affiliations=[str(aff) for aff in (item.get("affiliations") or [])],
            )
            for item in (value.get("authors") or [])
            if isinstance(item, dict) and item.get("name")
        ]
        return cls(
            arxiv_id=str(value.get("arxiv_id") or ""),
            title=str(value.get("title") or ""),
            authors=authors,
            abstract=str(value.get("abstract") or ""),
            categories=[str(item) for item in (value.get("categories") or [])],
            primary_category=str(value.get("primary_category") or ""),
            published=parse_date(value.get("published")),
            updated=parse_date(value.get("updated") or value.get("published")),
            abs_url=str(value.get("abs_url") or ""),
            pdf_url=str(value.get("pdf_url") or ""),
            version=int(value.get("version") or 1),
            doi=value.get("doi"),
            journal_ref=value.get("journal_ref"),
            comment=value.get("comment"),
            lexical_score=float(value.get("lexical_score") or 0),
            llm_score=value.get("llm_score"),
            feedback_score=float(value.get("feedback_score") or 0),
            recommendation_reason=str(value.get("recommendation_reason") or ""),
        )


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
