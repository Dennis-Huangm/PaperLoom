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
    published: datetime | None
    updated: datetime | None
    abs_url: str
    pdf_url: str
    version: int | None = 1
    doi: str | None = None
    journal_ref: str | None = None
    comment: str | None = None
    lexical_score: float = 0.0
    llm_score: float | None = None
    feedback_score: float = 0.0
    recommendation_reason: str = ""
    discovery_sources: list[str] = field(default_factory=lambda: ["arxiv"])
    metadata_source: str = "arxiv"
    metadata_status: str = "complete"
    abstract_kind: str = "full"
    resolution_note: str = ""

    @property
    def source_label(self) -> str:
        labels = {"arxiv": "arXiv", "alphaxiv": "alphaXiv"}
        source = " + ".join(labels.get(s, s) for s in self.discovery_sources)
        return f"{source or '未知来源'}发现" + (" · 待补全" if self.metadata_status != "complete" else "")

    @property
    def metadata_label(self) -> str:
        return {"arxiv": "arXiv", "alphaxiv": "alphaXiv（待补全）"}.get(self.metadata_source, "来源未核实")

    @property
    def published_sort_key(self) -> datetime:
        return self.published or datetime.min.replace(tzinfo=timezone.utc)

    @property
    def final_score(self) -> float:
        if self.llm_score is None:
            return self.lexical_score + self.feedback_score
        normalized_lexical = max(0.0, min(10.0, self.lexical_score))
        return 0.35 * normalized_lexical + 0.65 * self.llm_score + self.feedback_score

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["published"] = self.published.isoformat() if self.published else ""
        value["updated"] = self.updated.isoformat() if self.updated else ""
        value["final_score"] = self.final_score
        value["source_label"] = self.source_label
        return value

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "Paper":
        """Rehydrate a paper snapshot saved by recommendations or the library."""
        def parse_date(raw: Any) -> datetime | None:
            try:
                parsed = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
            except (TypeError, ValueError):
                return None
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)

        authors = [
            Author(
                name=str(item.get("name") or ""),
                affiliations=[str(aff) for aff in (item.get("affiliations") or [])],
            )
            for item in (value.get("authors") or [])
            if isinstance(item, dict) and item.get("name")
        ]
        legacy_alpha = "alphaXiv" in str(value.get("recommendation_reason") or "")
        complete = bool(value.get("authors") and value.get("categories") and value.get("abstract")
                        and parse_date(value.get("published")) and parse_date(value.get("updated")))
        partial = value.get("metadata_status", "partial" if legacy_alpha or not complete else "complete")
        source = value.get("metadata_source") or ("alphaxiv" if legacy_alpha else "arxiv" if complete else "unknown")
        return cls(
            arxiv_id=str(value.get("arxiv_id") or ""),
            title=str(value.get("title") or ""),
            authors=authors,
            abstract=str(value.get("abstract") or ""),
            categories=[str(item) for item in (value.get("categories") or [])],
            primary_category=str(value.get("primary_category") or ""),
            published=parse_date(value.get("published")),
            updated=parse_date(value.get("updated")),
            abs_url=str(value.get("abs_url") or ""),
            pdf_url=str(value.get("pdf_url") or ""),
            version=int(value["version"]) if value.get("version") and ("metadata_status" in value or partial == "complete") else None,
            doi=value.get("doi"),
            journal_ref=value.get("journal_ref"),
            comment=value.get("comment"),
            lexical_score=float(value.get("lexical_score") or 0),
            llm_score=value.get("llm_score"),
            feedback_score=float(value.get("feedback_score") or 0),
            recommendation_reason=str(value.get("recommendation_reason") or ""),
            discovery_sources=list(value.get("discovery_sources") or (["alphaxiv"] if legacy_alpha else ["arxiv"] if complete else [])),
            metadata_source=source,
            metadata_status=partial,
            abstract_kind=str(value.get("abstract_kind") or ("full" if partial == "complete" else "preview")),
            resolution_note=str(value.get("resolution_note") or ""),
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
