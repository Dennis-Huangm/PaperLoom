from __future__ import annotations

from contextlib import ExitStack
from functools import cached_property
import os

from .alphaxiv import AlphaXivClient
from .arxiv_client import ArxivClient
from .arxiv_html import ArxivHtmlFigureClient
from .config import AppConfig
from .llm import LLMClient
from .metadata import MetadataVerifier
from .pdf_pipeline import PDFParser
from .report import ReportGenerator


class ResearchClients:
    """Task-owned lazy dependencies; injected adapters remain caller-owned."""

    def __init__(self, config: AppConfig, *, arxiv=None, alphaxiv=None, arxiv_html=None,
                 llm=None, verifier=None, parser=None, reporter=None):
        self.config = config
        self._stack = ExitStack()
        self._closed = False
        for name, value in (("arxiv", arxiv), ("alphaxiv", alphaxiv), ("arxiv_html", arxiv_html),
                            ("llm", llm), ("verifier", verifier), ("parser", parser), ("reporter", reporter)):
            if value is not None:
                self.__dict__[name] = value

    def _own(self, factory):
        if self._closed:
            raise RuntimeError("任务的客户端已关闭")
        adapter = factory()
        client = getattr(adapter, "client", None)
        if client is not None:
            self._stack.callback(client.close)
        return adapter

    @cached_property
    def arxiv(self):
        return self._own(ArxivClient)

    @cached_property
    def alphaxiv(self):
        return self._own(lambda: AlphaXivClient(
            api_key=os.getenv(self.config.discovery.alphaxiv_api_key_env, ""),
            endpoint=self.config.discovery.alphaxiv_endpoint))

    @cached_property
    def arxiv_html(self):
        return self._own(ArxivHtmlFigureClient)

    @cached_property
    def llm(self):
        return self._own(lambda: LLMClient(self.config.llm))

    @cached_property
    def verifier(self):
        return self._own(lambda: MetadataVerifier(self.config.metadata))

    @cached_property
    def parser(self):
        return self._own(lambda: PDFParser(self.config.pdf))

    @cached_property
    def reporter(self):
        return self._own(lambda: ReportGenerator(self.llm, self.config.llm))

    def close(self):
        self._closed = True
        self._stack.close()

    def __enter__(self):
        if self._closed:
            raise RuntimeError("任务的客户端已关闭")
        return self

    def __exit__(self, *args):
        self.close()
