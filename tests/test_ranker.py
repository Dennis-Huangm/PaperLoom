from datetime import datetime, timezone

from arxiv_ra.config import DiscoveryConfig, RankingConfig
from arxiv_ra.models import Author, Paper
from arxiv_ra.ranker import matched_concept_groups, rank_papers


def make_paper(title: str, abstract: str) -> Paper:
    now = datetime.now(timezone.utc)
    return Paper(
        arxiv_id=title,
        title=title,
        authors=[Author("A")],
        abstract=abstract,
        categories=["cs.AI"],
        primary_category="cs.AI",
        published=now,
        updated=now,
        abs_url="https://example.test",
        pdf_url="https://example.test/a.pdf",
    )


def test_positive_and_negative_preferences() -> None:
    discovery = DiscoveryConfig(
        arxiv_categories=["cs.AI"],
        positive_keywords=["agent", "reasoning"],
        negative_keywords=["survey"],
    )
    ranking = RankingConfig()
    target = make_paper("Reasoning agent", "An agent with verifiable reasoning.")
    survey = make_paper("A survey", "A survey of agents.")
    ranked = rank_papers([survey, target], discovery, ranking)
    assert ranked[0] is target
    assert target.lexical_score > survey.lexical_score


def test_llm_score_is_bounded_with_lexical_score() -> None:
    paper = make_paper("p", "")
    paper.lexical_score = 100
    paper.llm_score = 0
    assert paper.final_score == 3.5


def test_agentic_t2i_requires_both_concept_axes() -> None:
    discovery = DiscoveryConfig(
        concept_groups=[
            ["agent", "llm", "self-correction"],
            ["text-to-image", "image generation", "diffusion"],
        ],
        minimum_concept_groups=2,
    )
    direct = make_paper("Agentic image generation", "An LLM agent controls a diffusion model.")
    generic_agent = make_paper("Database agent", "An LLM agent queries relational databases.")
    assert matched_concept_groups(direct, discovery) == 2
    assert matched_concept_groups(generic_agent, discovery) == 1
