from datetime import datetime, timezone
from types import SimpleNamespace
import pytest
import httpx

from paperloom.config import DiscoveryConfig
from paperloom.discovery.discovery import DiscoveryService
from paperloom.papers.models import Paper


def paper(aid="2609.00001", title="World models for robots", abstract="Robot control with world models."):
    now = datetime.now(timezone.utc)
    return Paper(aid, title, [], abstract, ["cs.RO"], "cs.RO", now, now,
                 f"https://arxiv.org/abs/{aid}", f"https://arxiv.org/pdf/{aid}")


def test_structured_queries_reserve_core_budget_and_merge_sources(tmp_path):
    calls = []
    def search(categories, days, limit, terms=None, *, query_groups=None):
        calls.append((limit, query_groups))
        return [paper()]
    config = DiscoveryConfig(provider="arxiv", arxiv_categories=["cs.RO"], max_candidates=10, search_plan={"version": 2, "branches": [
        {"id": "core", "label": "核心主题", "groups": [["world model", "world models"]]},
        {"id": "context", "label": "应用场景", "groups": [["world model"], ["robot"]]},
        {"id": "adjacent", "label": "相邻方法", "groups": [["world model"], ["planning"]]},
    ]})
    service = DiscoveryService(config, SimpleNamespace(search=search), SimpleNamespace(enabled=False), tmp_path)
    result = service.discover()
    assert [c[0] for c in calls] == [5, 3, 2]
    assert calls[1][1] == [["world model"], ["robot"]]
    assert len(result.papers) == 1
    assert set(result.papers[0].discovery_routes) == {"base", "base:core", "base:context", "base:adjacent"}
    assert result.sources["arxiv:core"]["limit"] == 5


@pytest.mark.parametrize("budget, expected", [(1, [1]), (2, [1, 1]), (3, [2, 1])])
def test_small_budgets_never_issue_zero_limit_requests(tmp_path, budget, expected):
    limits = []
    def search(*args, **kw):
        limits.append(args[2])
        return []
    branches = [{"id": name, "groups": [["world model"]]} for name in ("core", "context", "adjacent")]
    cfg = DiscoveryConfig(provider="arxiv", max_candidates=budget, search_plan={"version": 2, "branches": branches})
    DiscoveryService(cfg, SimpleNamespace(search=search), SimpleNamespace(enabled=False), tmp_path).discover()
    assert limits == expected


def test_arxiv_compiles_grouped_literals_and_rejects_raw_query_syntax():
    from paperloom.discovery.arxiv_client import ArxivClient
    queries = []
    def respond(request):
        queries.append(request.url.params["search_query"])
        return httpx.Response(200, text='<feed xmlns="http://www.w3.org/2005/Atom"/>')
    client = ArxivClient(min_interval=0)
    client.client.close()
    client.client = httpx.Client(transport=httpx.MockTransport(respond))
    try:
        client.search(["cs.RO"], 2, 10, query_groups=[["world model", "world models"], ["robot"]])
        assert '((all:"world model" OR all:"world models") AND (all:"robot"))' in queries[0]
        with pytest.raises(ValueError):
            client.search(["cs.RO"], 2, 10, query_groups=[['robot" OR all:*']])
        assert len(queries) == 1
    finally:
        client.client.close()


@pytest.mark.parametrize("abstract_length", [40, 4000])
def test_model_batches_isolate_failures_and_preserve_global_order(abstract_length):
    import json
    from paperloom.config import AppConfig
    from paperloom.discovery.plan_selection import evaluate_plan

    cfg = AppConfig()
    cfg.discovery.search_plan = {"version": 2, "conditions": [
        {"id": "robot", "kind": "required", "text": "robot", "confirmed": True}]}
    candidates = [paper(f"2611.{i:05}", abstract="robot" + "a" * (abstract_length - 5)) for i in range(46)]
    calls = []

    def chat(system, prompt, **kwargs):
        batch = json.loads(prompt.split("数据：")[-1])["papers"]
        calls.append([p["id"] for p in batch])
        assert len(batch) <= 20 and len(prompt) <= 32000
        if len(calls) == 2:
            raise RuntimeError("batch failed")
        return json.dumps({"papers": [{"id": p["id"], "score": 9 if p is batch[-1] else 7,
                            "conditions": [{"id": "robot", "verdict": "satisfied", "quote": "robot"}]}
                            for p in batch]})

    accepted, rejected = evaluate_plan(candidates, cfg, SimpleNamespace(enabled=True, chat=chat))
    assert len(calls) >= 3
    assert [aid for batch in calls for aid in batch] == [p.arxiv_id for p in candidates]
    assert {r["paper"]["arxiv_id"] for r in rejected} == set(calls[1])
    assert all("batch failed" in r["paper"]["ranking_explanation"]["condition_error"] for r in rejected)
    assert len(accepted) + len(rejected) == len(candidates)
    assert [p.llm_score for p in accepted] == sorted([p.llm_score for p in accepted], reverse=True)
