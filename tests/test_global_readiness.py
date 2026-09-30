"""Final daily-use review: publication provenance and local HTTP boundary."""
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from arxiv_ra.config import MetadataConfig
from arxiv_ra.metadata import MetadataVerifier, declared_venue_from_comment
from arxiv_ra.models import Paper, VerifiedMetadata
from arxiv_ra.report_metadata import protect_metadata
from arxiv_ra.web import create_app


def paper():
    now = datetime(2026, 9, 21, tzinfo=timezone.utc)
    return Paper("2609.00001", "Review paper", [], "", ["cs.AI"], "cs.AI",
                 now, now, "https://arxiv.org/abs/2609.00001", "https://arxiv.org/pdf/2609.00001")


@pytest.mark.parametrize("venue", ["", "arXiv", "CoRR", "arXiv (Cornell University)"])
def test_preprint_metadata_does_not_claim_formal_publication(venue):
    source = paper()
    result = VerifiedMetadata(title=source.title, doi="10.1234/published")
    verifier = MetadataVerifier(MetadataConfig())
    try:
        verifier._merge_semantic(result, {
            "title": source.title, "venue": venue, "publicationDate": "2026-09-21",
            "externalIds": {"DOI": "https://doi.org/10.48550/arXiv.2609.00001"},
        }, source)
    finally:
        verifier.client.close()
    assert result.publication_date is None
    assert result.venue is None
    assert result.doi == "10.1234/published"
    rendered = protect_metadata("# Report\n\n## Summary\nText", source, result)
    assert "| 正式发表日期 | 未核实 |" in rendered


def test_conflicting_venue_cannot_replace_original_publication_date():
    source = paper()
    result = VerifiedMetadata(title=source.title, venue="Journal A",
                              publication_date="2025-01-01", venue_status="verified_metadata")
    verifier = MetadataVerifier(MetadataConfig())
    try:
        verifier._merge_semantic(result, {
            "title": source.title, "venue": "Conference B", "publicationDate": "2026-09-21",
        }, source)
    finally:
        verifier.client.close()
    assert result.venue == "Journal A"
    assert result.publication_date == "2025-01-01"
    assert result.conflicts


def test_accepted_semantic_venue_keeps_publication_metadata():
    source = paper()
    result = VerifiedMetadata(title=source.title)
    verifier = MetadataVerifier(MetadataConfig())
    try:
        verifier._merge_semantic(result, {
            "title": source.title, "publicationVenue": {"name": "Journal A"},
            "publicationDate": "2026-09-25", "externalIds": {"DOI": "10.1234/published"},
        }, source)
    finally:
        verifier.client.close()
    assert (result.venue, result.publication_date, result.doi) == (
        "Journal A", "2026-09-25", "10.1234/published")


@pytest.mark.parametrize("comment", [
    "Submitted to CVPR 2026", "Under review at ICLR 2026",
    "Not accepted to ICML 2026", "CVPR 2026 submission",
    "We extend our CVPR 2025 paper with new experiments",
])
def test_conference_mention_is_not_a_publication_declaration(comment):
    assert declared_venue_from_comment(comment) is None


def test_acceptance_in_separate_clause_remains_a_declaration():
    assert declared_venue_from_comment(
        "Previously submitted to CVPR 2026; Accepted to ECCV 2026"
    ) == "ECCV 2026"


@pytest.fixture
def local_client(tmp_path):
    config = tmp_path / "config.yaml"
    config.write_text("output_dir: run\ndiscovery:\n  interest_description: Private research\n", encoding="utf-8")
    with TestClient(create_app(config)) as client:
        yield client


@pytest.mark.parametrize("path", ["/settings", "/generate", "/static/app.js"])
def test_nonlocal_host_cannot_read_local_app(local_client, path):
    response = local_client.get(path, headers={"Host": "attacker.example:8765"})
    assert response.status_code == 400
    assert "Private research" not in response.text


@pytest.mark.parametrize("host", ["localhost:8765", "127.0.0.1:8765", "[::1]:8765", "LOCALHOST:8765"])
def test_loopback_host_remains_usable(local_client, host):
    assert local_client.get("/static/app.js", headers={"Host": host}).status_code == 200


@pytest.mark.parametrize("origin", ["null", "https://attacker.example", "http://[invalid"])
def test_opaque_external_or_malformed_origin_cannot_mutate(local_client, origin):
    response = local_client.post("/settings/config", headers={"Origin": origin})
    assert response.status_code == 403


def test_local_origin_reaches_handler(local_client):
    # No form data: handler validation still runs for legitimate local requests.
    response = local_client.post("/settings/config", headers={"Origin": "http://localhost:8765"})
    assert response.status_code == 400


def test_profile_form_preserves_origin_on_same_origin_submission(local_client):
    page = local_client.get("/profiles")
    assert page.status_code == 200
    assert 'action="/profiles/create"' in page.text
    assert page.headers["Referrer-Policy"] == "same-origin"

    # An empty form reaches handler validation when the browser supplies its origin.
    response = local_client.post(
        "/profiles/create",
        headers={"Origin": "http://testserver", "Sec-Fetch-Site": "same-origin"},
    )
    assert response.status_code == 400
