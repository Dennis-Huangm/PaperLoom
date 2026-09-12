from datetime import datetime, timezone
from contextlib import contextmanager, nullcontext

from arxiv_ra.config import MetadataConfig
from arxiv_ra.metadata import MetadataVerifier, declared_venue_from_comment
from arxiv_ra.models import Author, Paper, VerifiedMetadata


def _paper() -> Paper:
    now = datetime.now(timezone.utc)
    return Paper(
        arxiv_id="2407.05600",
        title="GenArtist",
        authors=[Author("A")],
        abstract="",
        categories=["cs.CV"],
        primary_category="cs.CV",
        published=now,
        updated=now,
        abs_url="https://arxiv.org/abs/2407.05600",
        pdf_url="https://arxiv.org/pdf/2407.05600",
    )


def test_openalex_arxiv_repository_is_not_a_verified_venue() -> None:
    now = datetime.now(timezone.utc)
    paper = Paper(
        arxiv_id="2608.00001",
        title="Example",
        authors=[Author("A")],
        abstract="",
        categories=["cs.CV"],
        primary_category="cs.CV",
        published=now,
        updated=now,
        abs_url="https://arxiv.org/abs/2608.00001",
        pdf_url="https://arxiv.org/pdf/2608.00001",
    )
    result = VerifiedMetadata(title=paper.title)
    item = {
        "title": paper.title,
        "publication_date": "2026-08-01",
        "doi": "https://doi.org/10.48550/arxiv.2608.00001",
        "primary_location": {
            "source": {"display_name": "arXiv (Cornell University)", "type": "repository"}
        },
        "authorships": [],
    }
    MetadataVerifier(MetadataConfig())._merge_openalex(result, item, paper)
    assert result.venue is None
    assert result.venue_status == "unverified"
    assert result.publication_date is None
    assert result.doi is None


def test_declared_venue_from_arxiv_comment() -> None:
    assert declared_venue_from_comment("NeurIPS 2024 Spotlight") == "NeurIPS 2024 Spotlight"
    assert (
        declared_venue_from_comment("Accepted to ACM Transactions on Graphics (SIGGRAPH 2025)")
        == "ACM Transactions on Graphics (SIGGRAPH 2025)"
    )
    assert declared_venue_from_comment("Project page: https://example.com") is None


def test_semantic_scholar_retries_429_and_honors_retry_after(monkeypatch) -> None:
    class Response:
        def __init__(self, status_code, payload=None, headers=None):
            self.status_code = status_code
            self._payload = payload or {}
            self.headers = headers or {}

        def json(self):
            return self._payload

    class Client:
        def __init__(self):
            self.responses = [
                Response(429, headers={"Retry-After": "2"}),
                Response(200, {"title": "GenArtist"}),
            ]
            self.calls = 0

        def get(self, *args, **kwargs):
            response = self.responses[self.calls]
            self.calls += 1
            return response

    verifier = MetadataVerifier(
        MetadataConfig(semantic_scholar_min_interval=1.1, semantic_scholar_max_retries=3)
    )
    verifier.client = Client()
    monkeypatch.setattr(
        "arxiv_ra.metadata.shared_rate_limit", lambda *_args: nullcontext()
    )
    sleeps = []
    monkeypatch.setattr("arxiv_ra.metadata.time.sleep", sleeps.append)

    result = verifier._semantic_scholar(_paper())

    assert result == {"title": "GenArtist"}
    assert verifier.client.calls == 2
    assert sleeps == [2.0]


def test_semantic_scholar_uses_shared_rate_limit(monkeypatch) -> None:
    class Response:
        status_code = 200
        headers = {}

        @staticmethod
        def json():
            return {"title": "GenArtist"}

    class Client:
        @staticmethod
        def get(*args, **kwargs):
            return Response()

    slots = []

    @contextmanager
    def fake_slot(key, minimum):
        slots.append((key, minimum))
        yield

    verifier = MetadataVerifier(MetadataConfig(semantic_scholar_min_interval=1.1))
    verifier.client = Client()
    monkeypatch.setattr("arxiv_ra.metadata.shared_rate_limit", fake_slot)

    verifier._semantic_scholar(_paper())

    assert slots == [("semantic-scholar", 1.1)]
