from copy import deepcopy
from datetime import datetime, timezone
import json
from types import SimpleNamespace

import pymupdf
import pytest
import httpx

from arxiv_ra.direction_overview import OverviewStore, OverviewGenerator
from arxiv_ra.config import AppConfig
from arxiv_ra.models import Paper
from arxiv_ra.research_clients import ResearchClients
from arxiv_ra.arxiv_client import ArxivClient
from arxiv_ra.task_runtime import bind_task_hooks, TaskHooks, TaskCancelled


def direction():
    return {"id": "svg", "name": "SVG 生成", "description": "研究 SVG 生成与编辑",
            "discovery": {"positive_keywords": ["SVG"], "recommendation_count": 5},
            "references": [{"arxiv_id": "2401.00001v2"}]}


def test_overview_survives_topic_edit_and_failed_regeneration(tmp_path):
    store = OverviewStore(tmp_path)
    profile = direction()
    run = store.start(profile)
    store.publish(run, "# SVG 方向小综述\n\n旧文章", [], [])
    renamed = deepcopy(profile)
    renamed["name"] = "新的名称"
    renamed["discovery"]["recommendation_count"] = 9
    assert store.read(renamed)["stale"] is False

    changed = deepcopy(profile)
    changed["description"] = "仅研究 SVG 编辑"
    assert store.read(changed)["stale"] is True
    replacement = store.start(changed)
    store.fail(replacement, "模型暂时不可用")
    result = store.read(changed)
    assert result["status"] == "failed"
    assert "旧文章" in result["markdown"]
    assert result["stale"] is True
    assert "模型暂时不可用" in result["error"]


def literature(aid, year):
    return Paper(aid, "SVG editing " + aid, [], "A vector graphics editing method.", ["cs.CV"],
                 "cs.CV", datetime(year, 1, 1, tzinfo=timezone.utc), None,
                 f"https://arxiv.org/abs/{aid}v2", f"https://arxiv.org/pdf/{aid}v2", version=2)


class LiteratureAPI:
    def search_literature(self, *args, **kwargs):
        return [literature("2001.00001", 2020), literature("2501.00001", 2025), literature("2501.00002", 2025)]

    def get(self, aid):
        return literature(aid.removesuffix("v2"), 2024)

    def download_version(self, aid, version, destination):
        destination.parent.mkdir(parents=True, exist_ok=True)
        with pymupdf.open() as document:
            page = document.new_page()
            page.insert_text((40, 40), "SVG editing method: optimize paths and control points. " * 8)
            document.save(destination)


class OverviewModel:
    enabled = True

    def chat(self, system, prompt, **kwargs):
        if kwargs.get("json_mode"):
            return json.dumps({"selected": ["2401.00001", "2001.00001", "2501.00001"],
                               "excluded_references": [], "gaps": []})
        return "## 研究对象与问题边界\n\nSVG 编辑涉及控制点优化 [1]。\n\n## 主要技术路线\n\n方法对比 [2]。"


def test_generate_grounded_overview_and_keep_search_preferences_independent(tmp_path):
    profile = direction()
    profile["discovery"].update(lookback_days=1, mode="conference", conferences=["cvpr"])
    original = deepcopy(profile)
    config = AppConfig(output_dir="run")
    conferences = SimpleNamespace(discover=lambda *a, **kw: ([], {"cvpr": {"status": "empty"}}))
    clients = ResearchClients(config, arxiv=LiteratureAPI(), conferences=conferences, llm=OverviewModel())
    with OverviewGenerator(config, tmp_path, clients=clients) as generator:
        generator.generate(profile)
    result = OverviewStore(tmp_path / "run").read(profile)
    assert result["status"] == "ready"
    assert "[1](https://arxiv.org/abs/2401.00001v2)" in result["markdown"]
    assert "2001.00001v2" in result["markdown"]
    assert "## 参考文献" in result["markdown"]
    assert len([s for s in result["article"]["sources"] if s["material"] == "fulltext"]) == 3
    assert result["article"]["snapshot"]["reference_ids"] == ["2401.00001v2"]
    assert profile == original


def test_empty_retrieval_fails_without_inventing_an_introduction(tmp_path):
    profile = direction()
    profile["references"] = []
    config = AppConfig()
    arxiv = SimpleNamespace(search_literature=lambda *a, **kw: [])
    conferences = SimpleNamespace(discover=lambda *a, **kw: ([], {}))
    clients = ResearchClients(config, arxiv=arxiv, conferences=conferences, llm=OverviewModel())
    with OverviewGenerator(config, tmp_path, clients=clients) as generator:
        with pytest.raises(ValueError, match="材料不足"):
            generator.generate(profile)
    result = OverviewStore(tmp_path / "run").read(profile)
    assert result["status"] == "failed"
    assert result["article"] is None


def test_late_completion_is_bound_to_original_topic_and_survives_restart(tmp_path):
    profile = direction()
    store = OverviewStore(tmp_path)
    old = store.start(profile)
    changed = deepcopy(profile)
    changed["description"] = "仅研究 SVG 编辑"
    fresh = store.start(changed)
    store.publish(fresh, "# 新主题介绍", [], [])
    store.publish(old, "# 原主题介绍", [], [])
    restarted = OverviewStore(tmp_path)
    assert restarted.read(changed)["markdown"] == "# 新主题介绍"
    assert restarted.read(changed)["stale"] is False
    assert restarted.read(profile)["markdown"] == "# 原主题介绍"


def test_real_retrieval_uses_early_and_recent_scopes_and_delivers_abstract_fallback(tmp_path):
    from test_arxiv_client import ATOM
    queries = []

    def respond(request):
        if request.url.path == "/api/query":
            queries.append(dict(request.url.params))
            return httpx.Response(200, text=ATOM)
        return httpx.Response(503)

    class Model(OverviewModel):
        def chat(self, system, prompt, **kwargs):
            if kwargs.get("json_mode"):
                return '{"selected":["2608.01234"],"gaps":["仅有一篇相关论文"]}'
            return super().chat(system, prompt, **kwargs)

    arxiv = ArxivClient(min_interval=0, max_retries=0)
    arxiv.client.close()
    arxiv.client = httpx.Client(transport=httpx.MockTransport(respond))
    profile = direction()
    profile["references"] = []
    config = AppConfig()
    clients = ResearchClients(config, arxiv=arxiv, llm=Model(), conferences=SimpleNamespace(
        discover=lambda *a, **kw: ([], {})))
    try:
        with OverviewGenerator(config, tmp_path, clients=clients) as generator:
            generator.generate(profile)
        result = OverviewStore(tmp_path / "run").read(profile)
        assert len(queries) == 2
        assert all(q["sortBy"] == "relevance" for q in queries)
        assert "199101010000" in queries[1]["search_query"]
        assert str(datetime.now(timezone.utc).year - 3) in queries[0]["search_query"]
        assert result["status"] == "ready"
        assert result["article"]["sources"][0]["material"] == "abstract"
        assert "材料范围与不足" in result["markdown"]
    finally:
        arxiv.client.close()


def test_cancelling_during_generation_preserves_previous_article(tmp_path):
    profile = direction()
    config = AppConfig()
    store = OverviewStore(tmp_path / "run")
    store.publish(store.start(profile), "# 原有文章", [], [])
    cancelled = False

    class Model(OverviewModel):
        def chat(self, *args, **kwargs):
            nonlocal cancelled
            result = super().chat(*args, **kwargs)
            if not kwargs.get("json_mode"):
                cancelled = True
            return result

    clients = ResearchClients(config, arxiv=LiteratureAPI(), llm=Model(), conferences=SimpleNamespace(
        discover=lambda *a, **kw: ([], {})))
    hooks = TaskHooks(progress=lambda *a: None, warning=lambda *a: None, is_cancelled=lambda: cancelled)
    with OverviewGenerator(config, tmp_path, clients=clients) as generator, bind_task_hooks(hooks):
        with pytest.raises(TaskCancelled):
            generator.generate(profile)
    assert store.read(profile)["markdown"] == "# 原有文章"
    assert store.read(profile)["status"] == "cancelled"
