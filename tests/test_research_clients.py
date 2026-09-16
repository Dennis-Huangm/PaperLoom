from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from arxiv_ra.config import AppConfig
from arxiv_ra.pipeline import DailyPipeline
from arxiv_ra.research_clients import ResearchClients


def test_lazy_creation_and_exception_cleanup(monkeypatch):
    arxiv, llm = SimpleNamespace(client=Mock()), SimpleNamespace(client=Mock())
    arxiv_factory, llm_factory = Mock(return_value=arxiv), Mock(return_value=llm)
    parser_factory = Mock(side_effect=AssertionError("unused PDF parser must not be created"))
    monkeypatch.setattr("arxiv_ra.research_clients.ArxivClient", arxiv_factory)
    monkeypatch.setattr("arxiv_ra.research_clients.LLMClient", llm_factory)
    monkeypatch.setattr("arxiv_ra.research_clients.PDFParser", parser_factory)
    clients = ResearchClients(AppConfig())
    arxiv_factory.assert_not_called()
    with pytest.raises(ValueError):
        with clients:
            assert clients.arxiv is arxiv
            assert clients.llm is llm
            raise ValueError("failed task")
    arxiv.client.close.assert_called_once()
    llm.client.close.assert_called_once()
    parser_factory.assert_not_called()
    clients.close()
    arxiv.client.close.assert_called_once()


def test_borrowed_adapters_and_client_scope_remain_caller_owned(tmp_path):
    adapter = SimpleNamespace(client=Mock())
    clients = ResearchClients(AppConfig(), arxiv=adapter)
    with DailyPipeline(AppConfig(), tmp_path, clients=clients) as pipeline:
        assert pipeline.clients.arxiv is adapter
    assert not clients._closed
    clients.close()
    adapter.client.close.assert_not_called()


def test_pipeline_constructor_does_not_create_unused_dependencies(tmp_path, monkeypatch):
    forbidden = Mock(side_effect=AssertionError("eager network dependency"))
    for name in ("ArxivClient", "AlphaXivClient", "LLMClient", "PDFParser", "MetadataVerifier"):
        monkeypatch.setattr(f"arxiv_ra.research_clients.{name}", forbidden)
    with DailyPipeline(AppConfig(), tmp_path):
        pass
    forbidden.assert_not_called()
