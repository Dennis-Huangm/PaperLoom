from __future__ import annotations

import html
import json
import math
import os
import re
import time
from collections import Counter
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx

from . import __version__
from .config import AppConfig
from .rate_limit import shared_rate_limit
from .utils import write_json


class CitationExplorer:
    def __init__(self, config: AppConfig, project_root: Path) -> None:
        self.config = config
        output = Path(config.output_dir)
        self.output_root = output if output.is_absolute() else project_root / output
        self.client = httpx.Client(timeout=30.0, follow_redirects=True)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.client.close()

    def generate(self, arxiv_id: str) -> Path:
        folder = self.output_root / "citations" / f"{arxiv_id.replace('/', '-')}-{self.config.profile_id or 'default'}"
        existing_path = folder / "graph.json"
        try:
            existing = json.loads(existing_path.read_text(encoding="utf-8")) if existing_path.exists() else {}
        except (OSError, json.JSONDecodeError):
            existing = {}
        paper_id = f"ARXIV:{arxiv_id}"
        fields = "paperId,title,abstract,year,venue,citationCount,externalIds,url,authors"
        try:
            seed = self._get(
                f"https://api.semanticscholar.org/graph/v1/paper/{quote(paper_id, safe=':')}",
                {"fields": fields},
            )
        except httpx.HTTPError:
            seed = existing.get("seed") or {}
        if not seed or not seed.get("paperId"):
            raise LookupError(f"Semantic Scholar 未找到 arXiv:{arxiv_id}")
        try:
            references_payload = self._get(
                f"https://api.semanticscholar.org/graph/v1/paper/{seed['paperId']}/references",
                {"fields": fields, "limit": self.config.citations.max_references},
            )
            references = self._nodes(references_payload, "citedPaper")
        except httpx.HTTPError:
            references = existing.get("references") or []
        try:
            citations_payload = self._get(
                f"https://api.semanticscholar.org/graph/v1/paper/{seed['paperId']}/citations",
                {"fields": fields, "limit": self.config.citations.max_citations},
            )
            citations = self._nodes(citations_payload, "citingPaper")
        except httpx.HTTPError:
            citations = existing.get("citations") or []
        try:
            similar_payload = self._get(
                f"https://api.semanticscholar.org/recommendations/v1/papers/forpaper/{seed['paperId']}",
                {"fields": fields, "limit": self.config.citations.max_similar, "from": "all-cs"},
            )
            similar = [
                item for item in (similar_payload or {}).get("recommendedPapers", [])
                if item and item.get("title")
            ]
        except httpx.HTTPError:
            similar = existing.get("similar") or []
        graph = {
            "arxiv_id": arxiv_id,
            "seed": seed,
            "references": references,
            "citations": citations,
            "similar": similar,
        }
        graph["nodes"] = self._combined_nodes(graph)
        graph["citation_edges"] = self._cross_edges(graph)
        if not any(edge.get("kind") == "cross-citation" for edge in graph["citation_edges"]):
            node_ids = {str(node.get("paperId") or "") for node in graph["nodes"]}
            known = {(edge["source"], edge["target"]) for edge in graph["citation_edges"]}
            for edge in (existing.get("citation_edges") or existing.get("edges") or []):
                pair = (str(edge.get("source") or ""), str(edge.get("target") or ""))
                if (
                    edge.get("kind") == "cross-citation"
                    and pair[0] in node_ids
                    and pair[1] in node_ids
                    and pair not in known
                ):
                    graph["citation_edges"].append(edge)
        graph["edges"] = self._similarity_edges(graph["nodes"])
        folder.mkdir(parents=True, exist_ok=True)
        write_json(folder / "graph.json", graph)
        destination = folder / "index.html"
        destination.write_text(self._render(graph), encoding="utf-8")
        return destination

    def _get(self, url: str, params: dict[str, Any]) -> dict[str, Any]:
        return self._request("GET", url, params=params)

    def _post(self, url: str, params: dict[str, Any], payload: dict[str, Any]) -> Any:
        return self._request("POST", url, params=params, payload=payload)

    def _request(
        self,
        method: str,
        url: str,
        params: dict[str, Any],
        payload: dict[str, Any] | None = None,
    ) -> Any:
        attempts = max(1, self.config.citations.max_retries + 1)
        headers = {"User-Agent": f"PaperLoom/{__version__}"}
        key = os.getenv(self.config.metadata.semantic_scholar_api_key_env)
        if key:
            headers["x-api-key"] = key
        with shared_rate_limit("semantic-scholar", self.config.citations.min_interval):
            for attempt in range(attempts):
                response = self.client.request(
                    method, url, params=params, json=payload, headers=headers
                )
                if response.status_code == 200:
                    return response.json()
                if response.status_code != 429 or attempt == attempts - 1:
                    response.raise_for_status()
                retry_after = response.headers.get("Retry-After", "")
                try:
                    delay = float(retry_after)
                except ValueError:
                    delay = float(2**attempt)
                time.sleep(min(max(delay, 0), 30))
        return {}

    @staticmethod
    def _combined_nodes(graph: dict[str, Any]) -> list[dict[str, Any]]:
        by_id: dict[str, dict[str, Any]] = {}

        def add(item: dict[str, Any], role: str) -> None:
            paper_id = str(item.get("paperId") or "")
            if not paper_id:
                return
            if paper_id not in by_id:
                by_id[paper_id] = {**item, "roles": []}
            if role not in by_id[paper_id]["roles"]:
                by_id[paper_id]["roles"].append(role)

        add(graph["seed"], "seed")
        for item in graph["references"]:
            add(item, "reference")
        for item in graph["citations"]:
            add(item, "citation")
        for item in graph["similar"]:
            add(item, "similar")
        return list(by_id.values())

    def _cross_edges(self, graph: dict[str, Any]) -> list[dict[str, str]]:
        seed_id = str(graph["seed"].get("paperId") or "")
        node_ids = {str(item.get("paperId") or "") for item in graph["nodes"]}
        edges: dict[tuple[str, str], dict[str, str]] = {}

        def add(source: str, target: str, kind: str) -> None:
            if not source or not target or source == target:
                return
            key = (source, target)
            current = edges.get(key)
            if current is None or current["kind"] == "similar":
                edges[key] = {"source": source, "target": target, "kind": kind}

        for item in graph["references"]:
            add(seed_id, str(item.get("paperId") or ""), "reference")
        for item in graph["citations"]:
            add(str(item.get("paperId") or ""), seed_id, "citation")
        for item in graph["similar"]:
            paper_id = str(item.get("paperId") or "")
            if (seed_id, paper_id) not in edges and (paper_id, seed_id) not in edges:
                add(seed_id, paper_id, "similar")
        try:
            batch = self._post(
                "https://api.semanticscholar.org/graph/v1/paper/batch",
                {"fields": "paperId,references.paperId"},
                {"ids": sorted(node_ids)},
            )
            for item in batch if isinstance(batch, list) else []:
                source = str((item or {}).get("paperId") or "")
                for reference in (item or {}).get("references") or []:
                    target = str((reference or {}).get("paperId") or "")
                    if source in node_ids and target in node_ids:
                        add(source, target, "cross-citation")
        except Exception:
            pass
        return list(edges.values())

    @staticmethod
    def _similarity_edges(nodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
        stopwords = {
            "the", "a", "an", "and", "or", "of", "to", "in", "on", "for", "with",
            "from", "by", "via", "using", "based", "we", "this", "that", "our", "is",
            "are", "be", "as", "at", "it", "can", "new", "paper", "method", "model",
            "models", "approach", "framework", "system", "results", "task", "tasks",
        }

        def term_counts(node: dict[str, Any]) -> Counter[str]:
            title_tokens = [
                token
                for token in re.findall(
                    r"[a-z][a-z0-9-]+", str(node.get("title") or "").casefold()
                )
                if len(token) > 2 and token not in stopwords
            ]
            abstract_tokens = [
                token
                for token in re.findall(
                    r"[a-z][a-z0-9-]+", str(node.get("abstract") or "").casefold()
                )
                if len(token) > 2 and token not in stopwords
            ]
            title_bigrams = [
                f"{title_tokens[index]} {title_tokens[index + 1]}"
                for index in range(len(title_tokens) - 1)
            ]
            abstract_bigrams = [
                f"{abstract_tokens[index]} {abstract_tokens[index + 1]}"
                for index in range(len(abstract_tokens) - 1)
            ]
            return Counter(
                title_tokens * 3
                + title_bigrams * 3
                + abstract_tokens
                + abstract_bigrams
            )

        counts = [term_counts(node) for node in nodes]
        document_frequency: Counter[str] = Counter()
        for item in counts:
            document_frequency.update(item.keys())
        total = max(1, len(nodes))
        vectors: list[dict[str, float]] = []
        norms: list[float] = []
        for item in counts:
            vector = {
                term: (1 + math.log(frequency))
                * (math.log((1 + total) / (1 + document_frequency[term])) + 1)
                for term, frequency in item.items()
            }
            vectors.append(vector)
            norms.append(math.sqrt(sum(value * value for value in vector.values())) or 1.0)

        similarities: dict[tuple[int, int], float] = {}
        for left in range(len(nodes)):
            for right in range(left + 1, len(nodes)):
                small, large = (
                    (vectors[left], vectors[right])
                    if len(vectors[left]) <= len(vectors[right])
                    else (vectors[right], vectors[left])
                )
                similarities[(left, right)] = sum(
                    value * large.get(term, 0.0) for term, value in small.items()
                ) / (norms[left] * norms[right])

        chosen: dict[tuple[int, int], float] = {}
        for index in range(len(nodes)):
            ranked = sorted(
                (
                    (score, right if left == index else left)
                    for (left, right), score in similarities.items()
                    if left == index or right == index
                ),
                reverse=True,
            )
            selected = [(score, other) for score, other in ranked[:4] if score >= 0.025]
            if not selected and ranked:
                selected = [ranked[0]]
            for score, other in selected:
                pair = tuple(sorted((index, other)))
                chosen[pair] = max(chosen.get(pair, 0.0), score)

        return [
            {
                "source": str(nodes[left].get("paperId") or ""),
                "target": str(nodes[right].get("paperId") or ""),
                "kind": "similarity",
                "score": round(score, 5),
            }
            for (left, right), score in chosen.items()
            if nodes[left].get("paperId") and nodes[right].get("paperId")
        ]

    @staticmethod
    def _nodes(payload: dict[str, Any], field: str) -> list[dict[str, Any]]:
        return [
            item[field]
            for item in (payload or {}).get("data", [])
            if item.get(field) and item[field].get("title")
        ]

    @staticmethod
    def _paper_url(item: dict[str, Any]) -> str:
        external = item.get("externalIds") or {}
        arxiv_id = external.get("ArXiv")
        return f"https://arxiv.org/abs/{arxiv_id}" if arxiv_id else str(item.get("url") or "#")

    def _cards(self, items: list[dict[str, Any]]) -> str:
        cards: list[str] = []
        for item in items:
            authors = ", ".join(str(author.get("name") or "") for author in (item.get("authors") or [])[:3])
            cards.append(
                f"""<article><a href="{html.escape(self._paper_url(item))}" target="_blank" rel="noreferrer"><h3>{html.escape(str(item.get('title') or ''))}</h3></a>
<p>{html.escape(authors)} · {html.escape(str(item.get('year') or '年份未知'))} · 引用 {int(item.get('citationCount') or 0)}</p>
<span>{html.escape(str(item.get('venue') or '出版信息未核实'))}</span></article>"""
            )
        return "".join(cards) or "<div class='empty'>暂无可用结果</div>"

    def _render(self, graph: dict[str, Any]) -> str:
        seed = graph["seed"]
        title = html.escape(str(seed.get("title") or graph["arxiv_id"]))
        graph_json = json.dumps(graph, ensure_ascii=False).replace("</", "<\\/")
        page = r"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>__TITLE__ · 论文关系图</title>
<style>
:root{--ink:#202a32;--muted:#707b84;--line:#dfe3e5;--accent:#b96825;--blue:#315f84;--purple:#85497f;--surface:#fff;--bg:#f5f6f4}*{box-sizing:border-box;min-width:0}html,body{height:100%;margin:0;font-family:"Segoe UI","Noto Sans SC","Microsoft YaHei",sans-serif;color:var(--ink);background:var(--bg)}button,input,select{font:inherit}.topbar{height:64px;background:#fff;border-bottom:1px solid var(--line);display:grid;grid-template-columns:300px minmax(0,1fr) auto;align-items:center;padding:0 20px;gap:24px}.brand{display:flex;align-items:center;gap:11px;color:#28577c;font-weight:800;text-decoration:none;font-size:18px}.brand-mark{width:33px;height:33px;display:grid;place-items:center;color:#fff;background:linear-gradient(145deg,#4d8f93,#315f84);border-radius:50%;font-size:13px}.seed-title{font-size:14px;font-weight:700;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.top-actions{display:flex;gap:8px}.top-actions button,.top-actions a{height:34px;border:1px solid var(--line);background:#fff;border-radius:4px;padding:0 11px;display:flex;align-items:center;color:#55616a;text-decoration:none;cursor:pointer}.workspace{height:calc(100vh - 64px);display:grid;grid-template-columns:330px minmax(520px,1fr) 390px;background:#fff}.left-panel,.right-panel{overflow:auto;background:#fff;z-index:2}.left-panel{border-right:1px solid var(--line)}.right-panel{border-left:1px solid var(--line)}.origin{padding:18px 20px;background:#fbf6fb;border-bottom:1px solid #e4d8e5}.origin span{font-size:11px;color:var(--purple);font-weight:800;text-transform:uppercase}.origin h1{font-size:17px;line-height:1.35;margin:6px 0}.origin p{font-size:11px;color:var(--muted);margin:0}.search-wrap{padding:12px;border-bottom:1px solid var(--line)}.search-wrap input{width:100%;height:38px;border:1px solid var(--line);border-radius:4px;padding:0 11px}.tabs{display:grid;grid-template-columns:repeat(4,1fr);border-bottom:1px solid var(--line)}.tabs button{height:38px;border:0;border-right:1px solid var(--line);background:#fff;color:#69737c;font-size:10px;cursor:pointer}.tabs button.active{color:var(--blue);background:#eef5f7;font-weight:800}.paper-list article{padding:13px 16px;border-bottom:1px solid var(--line);cursor:pointer}.paper-list article:hover,.paper-list article.active{background:#f3f7f8}.paper-list h3{font-size:12px;line-height:1.35;margin:0 0 5px}.paper-list p{font-size:10px;color:var(--muted);margin:0}.role-pills{display:flex;gap:4px;margin-top:7px;flex-wrap:wrap}.role-pills i{font-style:normal;font-size:8px;padding:2px 5px;border-radius:10px;background:#edf1f2;color:#62717a}.graph-panel{position:relative;overflow:hidden;background:radial-gradient(circle at center,#fff 0,#fbfcfb 62%,#f4f6f5 100%)}#graph{width:100%;height:100%;display:block;touch-action:none}.edge{stroke:#718088;stroke-opacity:.22;stroke-width:1}.edge.reference,.edge.citation{stroke:#465d67;stroke-opacity:.45;stroke-width:1.25}.edge.similar{stroke:#8ca2a4;stroke-dasharray:3 4;stroke-opacity:.28}.edge.cross-citation{stroke:#536b74;stroke-opacity:.34}.node{cursor:pointer}.node circle{stroke:#fff;stroke-width:1.5;transition:.15s}.node:hover circle,.node.selected circle{stroke:#b96825;stroke-width:3}.node.seed circle{stroke:var(--purple);stroke-width:5}.node text{font-size:10px;fill:#27333b;paint-order:stroke;stroke:#fff;stroke-width:3;stroke-linejoin:round;pointer-events:none}.graph-toolbar{position:absolute;top:14px;left:14px;right:14px;display:flex;align-items:center;gap:8px;pointer-events:none}.graph-toolbar>*{pointer-events:auto}.graph-toolbar button,.graph-toolbar label{height:34px;border:1px solid var(--line);background:rgba(255,255,255,.94);border-radius:4px;padding:0 10px;color:#58656d;font-size:10px;display:flex;align-items:center;gap:6px}.graph-toolbar button{cursor:pointer}.graph-toolbar label:last-child{margin-left:auto}.graph-toolbar input{width:90px}.legend{position:absolute;left:20px;right:20px;bottom:16px;display:flex;align-items:end;justify-content:space-between;pointer-events:none}.year-scale{width:250px}.gradient{height:10px;background:linear-gradient(90deg,#dce5e5,#244e52);margin-bottom:4px}.year-labels{display:flex;justify-content:space-between;font-size:10px;color:#53616a}.legend-copy{display:flex;gap:12px;font-size:9px;color:#65717a;background:rgba(255,255,255,.88);padding:6px 8px}.legend-copy span:before{content:"";width:8px;height:8px;border-radius:50%;display:inline-block;margin-right:4px;background:#53787b}.legend-copy .origin-key:before{background:#8a4d82}.details{padding:22px 24px}.details .eyebrow{font-size:10px;color:var(--blue);font-weight:800;text-transform:uppercase}.details h2{font-size:22px;line-height:1.3;margin:8px 0 12px}.details .authors{font-size:12px;color:#606c75}.details .meta{display:flex;flex-wrap:wrap;gap:8px;margin:14px 0}.details .meta span{font-size:10px;background:#f0f3f3;padding:4px 7px;border-radius:3px}.details .abstract{font-size:13px;line-height:1.7;margin-top:20px;color:#37434b}.details .links{display:flex;gap:8px;margin-top:18px}.details .links a{border:1px solid var(--line);border-radius:4px;padding:7px 10px;text-decoration:none;color:var(--blue);font-size:10px}.details .roles{margin-top:14px}.empty-list{padding:24px;color:var(--muted);font-size:11px}@media(max-width:1150px){.workspace{grid-template-columns:260px minmax(440px,1fr) 320px}.topbar{grid-template-columns:230px minmax(0,1fr) auto}}@media(max-width:820px){.topbar{grid-template-columns:1fr auto}.seed-title{display:none}.workspace{height:auto;grid-template-columns:1fr}.graph-panel{height:65vh;grid-row:1}.left-panel,.right-panel{max-height:55vh;border:0;border-top:1px solid var(--line)}.top-actions button{display:none}}
.topbar{height:62px}.brand{color:var(--ink);font-size:15px}.brand-mark{border-radius:6px;background:#c7650e}.origin{background:#fff8ef;border-bottom-color:#ead9c9}.origin span,.details .eyebrow{color:#c7650e}.tabs button.active{color:#a95108;background:#fff3e7}.paper-list article:hover,.paper-list article.active{background:#fff8ef;box-shadow:inset 3px 0 #c7650e}.role-pills i{background:#f2f0ec;color:#68645f}.graph-panel{background-color:#f8f8f6;background-image:radial-gradient(#deddd9 .7px,transparent .7px);background-size:18px 18px}.edge{stroke:#777f82}.edge.similarity{stroke:#686f72}.edge.citation-overlay{stroke:#c7650e;stroke-opacity:.58;stroke-width:1.35;stroke-dasharray:6 4}.node circle{stroke:#f8f8f6;stroke-width:2}.node:hover circle,.node.selected circle{stroke:#c7650e;stroke-width:3}.node.seed circle{stroke:#5b3b24;stroke-width:4}.node text{font-size:10.5px;fill:#30363a;stroke:#f8f8f6;stroke-width:5;stroke-opacity:.98}.graph-toolbar button,.graph-toolbar label{border-color:#d9dcdf;background:rgba(255,255,255,.95);color:#59626b}.graph-toolbar button.active{border-color:#c7650e;background:#fff3e7;color:#a95108}.gradient{background:linear-gradient(90deg,#ddd8cf,#555d61)}.legend-copy{color:#66615b}.legend-copy span:before{background:#777d7f}.legend-copy .origin-key:before{background:#c7650e}.details .links a{color:#c7650e}.details .meta span{background:#f3f1ed}.workspace{height:calc(100vh - 62px)}.graph-help{width:min(560px,calc(100vw - 28px));border:1px solid #d9dcdf;border-radius:6px;padding:0;color:#20262d;background:#fff;box-shadow:0 24px 80px rgba(30,34,38,.25)}.graph-help::backdrop{background:rgba(30,34,38,.4)}.graph-help header{display:flex;justify-content:space-between;align-items:center;padding:16px 20px;border-bottom:1px solid #d9dcdf}.graph-help h2{font-size:19px;margin:0}.graph-help header button{border:0;background:transparent;font-size:23px;color:#717982;cursor:pointer}.graph-help div{padding:18px 24px}.graph-help p{margin:0 0 12px}.graph-help li{margin:9px 0;line-height:1.6}.graph-help strong{color:#c7650e}
</style></head><body><header class="topbar"><a class="brand" href="/citations"><span class="brand-mark">●</span>Research Graph</a><div class="seed-title">__TITLE__</div><div class="top-actions"><button id="graph-help-open">如何阅读</button><button id="toggle-labels">隐藏标签</button><a href="/citations">返回图谱库</a></div></header>
<main class="workspace"><aside class="left-panel"><div class="origin"><span>Origin paper</span><h1>__TITLE__</h1><p id="origin-meta"></p></div><div class="search-wrap"><input id="paper-search" type="search" placeholder="搜索标题或作者"></div><div class="tabs"><button class="active" data-role="all">全部</button><button data-role="reference">前作</button><button data-role="citation">后续</button><button data-role="similar">相似</button></div><div class="paper-list" id="paper-list"></div></aside>
<section class="graph-panel"><div class="graph-toolbar"><button id="reset-view">重置视图</button><button id="center-seed">定位起点</button><button class="active" id="toggle-similarity">隐藏相似线</button><button id="toggle-citations">显示引用边</button><label>最低引用 <input id="citation-filter" type="range" min="0" max="100" value="0"><b id="citation-value">0</b></label></div><svg id="graph" viewBox="0 0 1200 820" aria-label="论文关系图"><g id="viewport"><g id="edges"></g><g id="nodes"></g></g></svg><div class="legend"><div class="year-scale"><div class="gradient"></div><div class="year-labels"><span id="min-year"></span><span id="max-year"></span></div></div><div class="legend-copy"><span class="origin-key">起点论文</span><span>颜色 = 年份</span><span>灰色粗线 = 更相似</span><span>橙色虚线 = 引用</span></div></div></section>
<aside class="right-panel"><div class="details"><span class="eyebrow" id="detail-role">Selected paper</span><h2 id="detail-title"></h2><p class="authors" id="detail-authors"></p><div class="meta" id="detail-meta"></div><div class="roles" id="detail-roles"></div><div class="links" id="detail-links"></div><p class="abstract" id="detail-abstract"></p></div></aside></main><dialog class="graph-help" id="graph-help"><header><h2>如何阅读这张图</h2><button id="graph-help-close" aria-label="关闭">×</button></header><div><p>每个节点代表一篇与起点论文相关的学术论文。</p><ul><li>论文按照<strong>标题与摘要的语义相似度</strong>排列，而不是引用树。</li><li>节点大小代表论文的引用数。</li><li>节点颜色代表发表年份。</li><li>越相似的论文连线越粗、距离越近，并自然聚成簇。</li><li>引用关系是可选叠加层，默认不参与布局。</li></ul><p>本项目使用本地图内 TF-IDF 相似度近似语义空间，与 Connected Papers 的专有嵌入算法并不相同。</p></div></dialog>
<script id="graph-data" type="application/json">__GRAPH_DATA__</script><script>
const data=JSON.parse(document.querySelector('#graph-data').textContent);const nodes=data.nodes.map((n,i)=>({...n,index:i,x:0,y:0,vx:0,vy:0}));const byId=new Map(nodes.map(n=>[n.paperId,n]));const edges=data.edges.map(e=>({...e,sourceNode:byId.get(e.source),targetNode:byId.get(e.target)})).filter(e=>e.sourceNode&&e.targetNode);const citationEdges=(data.citation_edges||[]).map(e=>({...e,sourceNode:byId.get(e.source),targetNode:byId.get(e.target)})).filter(e=>e.sourceNode&&e.targetNode);const similarityScores=edges.map(e=>Number(e.score)||0),minSimilarity=Math.min(...similarityScores),maxSimilarity=Math.max(...similarityScores);edges.forEach(e=>e.strength=.15+.85*((Number(e.score)||0)-minSimilarity)/Math.max(.0001,maxSimilarity-minSimilarity));const seed=nodes.find(n=>n.roles.includes('seed'))||nodes[0];const years=nodes.map(n=>Number(n.year)).filter(Boolean);const fallbackYear=new Date().getFullYear(),minYear=years.length?Math.min(...years):fallbackYear,maxYear=years.length?Math.max(...years):fallbackYear;document.querySelector('#min-year').textContent=minYear;document.querySelector('#max-year').textContent=maxYear;document.querySelector('#origin-meta').textContent=`${(seed.authors||[]).slice(0,3).map(a=>a.name).join(', ')} · ${seed.year||'年份未知'}`;
const hash=s=>[...String(s)].reduce((a,c)=>(a*31+c.charCodeAt(0))>>>0,2166136261);const simRadius=n=>n.roles.includes('seed')?24:Math.max(7,Math.min(27,7+Math.sqrt(Number(n.citationCount)||0)*.95));const golden=Math.PI*(3-Math.sqrt(5));const ordered=nodes.filter(n=>n!==seed).sort((a,b)=>hash(a.paperId)-hash(b.paperId));ordered.forEach((n,i)=>{const progress=(i+.7)/ordered.length,angle=i*golden+(hash(n.title)%100)/250,r=95+Math.sqrt(progress)*300;n.x=600+Math.cos(angle)*r;n.y=410+Math.sin(angle)*r*.78});seed.x=600;seed.y=410;
for(let step=0;step<980;step++){for(const n of nodes){if(n===seed)continue;n.vx+=(600-n.x)*.00105;n.vy+=(410-n.y)*.00105;const radial=Math.hypot(n.x-600,(n.y-410)/.82);if(radial>350){n.vx+=(600-n.x)*.0019;n.vy+=(410-n.y)*.0019}}for(let i=0;i<nodes.length;i++)for(let j=i+1;j<nodes.length;j++){const a=nodes[i],b=nodes[j],dx=a.x-b.x,dy=a.y-b.y,d=Math.sqrt(dx*dx+dy*dy)||1,d2=d*d,unitX=dx/d,unitY=dy/d,repel=2300/(d2+60);if(a!==seed){a.vx+=unitX*repel;a.vy+=unitY*repel}if(b!==seed){b.vx-=unitX*repel;b.vy-=unitY*repel}const minimum=simRadius(a)+simRadius(b)+26;if(d<minimum){const push=(minimum-d)*.03;if(a!==seed){a.vx+=unitX*push;a.vy+=unitY*push}if(b!==seed){b.vx-=unitX*push;b.vy-=unitY*push}}}for(const e of edges){const a=e.sourceNode,b=e.targetNode,dx=b.x-a.x,dy=b.y-a.y,d=Math.sqrt(dx*dx+dy*dy)||1,ideal=235-e.strength*115,force=(d-ideal)*(.0017+e.strength*.0048);if(a!==seed){a.vx+=dx/d*force;a.vy+=dy/d*force}if(b!==seed){b.vx-=dx/d*force;b.vy-=dy/d*force}}for(const n of nodes){if(n===seed){n.x=600;n.y=410;n.vx=0;n.vy=0;continue}n.vx=Math.max(-7,Math.min(7,n.vx*.8));n.vy=Math.max(-7,Math.min(7,n.vy*.8));n.x=Math.max(95,Math.min(1105,n.x+n.vx));n.y=Math.max(100,Math.min(710,n.y+n.vy))}}
const svg=document.querySelector('#graph'),viewport=document.querySelector('#viewport'),edgeLayer=document.querySelector('#edges'),nodeLayer=document.querySelector('#nodes');const NS='http://www.w3.org/2000/svg';let selected=seed,roleFilter='all',query='',minCitations=0,showLabels=true,showSimilarity=true,showCitations=false,transform={x:0,y:0,k:1};const radius=simRadius;const color=n=>{const t=((Number(n.year)||minYear)-minYear)/Math.max(1,maxYear-minYear),from=[221,216,207],to=[85,93,97],rgb=from.map((v,i)=>Math.round(v+(to[i]-v)*t));return `rgb(${rgb.join(',')})`};const baseLabels=nodes.map(n=>{const author=(n.authors||[])[0]?.name?.split(/\s+/).slice(-1)[0]||'Paper';return `${author}, ${n.year||''}`});const labelTotals=new Map;for(const label of baseLabels)labelTotals.set(label,(labelTotals.get(label)||0)+1);const labelSeen=new Map;nodes.forEach((n,i)=>{const base=baseLabels[i],seen=labelSeen.get(base)||0;labelSeen.set(base,seen+1);n.shortLabel=labelTotals.get(base)>1?`${base}${String.fromCharCode(97+seen)}`:base});
function placeLabels(){const boxes=[];const disks=nodes.map(n=>({x:n.x,y:n.y,r:radius(n)+5}));const intersectsDisk=(box,disk)=>{const cx=Math.max(box.x,Math.min(disk.x,box.x+box.w)),cy=Math.max(box.y,Math.min(disk.y,box.y+box.h));return Math.hypot(cx-disk.x,cy-disk.y)<disk.r};const sorted=[seed,...nodes.filter(n=>n!==seed).sort((a,b)=>(b.citationCount||0)-(a.citationCount||0))];for(const n of sorted){const w=Math.max(34,n.shortLabel.length*6.2),h=14,outward=Math.atan2((n.y-410)/.82,n.x-600);let chosen=null;for(let layer=0;layer<7&&!chosen;layer++){const distance=radius(n)+14+layer*14;const angles=[outward,-Math.PI/2,Math.PI/2,0,Math.PI,-Math.PI/4,-3*Math.PI/4,Math.PI/4,3*Math.PI/4];for(const angle of angles){const cx=n.x+Math.cos(angle)*distance,cy=n.y+Math.sin(angle)*distance,box={x:cx-w/2,y:cy-h/2,w,h};if(box.x<10||box.x+box.w>1190||box.y<48||box.y+box.h>790)continue;if(boxes.some(other=>box.x<other.x+other.w+3&&box.x+box.w+3>other.x&&box.y<other.y+other.h+2&&box.y+box.h+2>other.y))continue;if(disks.some(d=>intersectsDisk(box,d)))continue;chosen={box,cx,cy};break}}if(!chosen){const distance=radius(n)+Math.hypot(w/2,h/2)+12,cx=n.x+Math.cos(outward)*distance,cy=n.y+Math.sin(outward)*distance;chosen={box:{x:cx-w/2,y:cy-h/2,w,h},cx,cy}}boxes.push(chosen.box);n.labelX=chosen.cx;n.labelY=chosen.cy}return boxes}const labelBoxes=placeLabels();
const edgeEls=edges.map(e=>{const line=document.createElementNS(NS,'line');line.classList.add('edge','similarity');line.style.strokeWidth=String(.45+e.strength*2.7);line.style.strokeOpacity=String(.06+e.strength*.56);edgeLayer.append(line);return {e,line}});const citationEdgeEls=citationEdges.map(e=>{const line=document.createElementNS(NS,'line');line.classList.add('edge','citation-overlay');line.style.display='none';edgeLayer.append(line);return {e,line}});const nodeEls=nodes.map(n=>{const g=document.createElementNS(NS,'g');g.classList.add('node',...n.roles);const c=document.createElementNS(NS,'circle');c.setAttribute('r',radius(n));c.setAttribute('fill',n.roles.includes('seed')?'#c7650e':color(n));const text=document.createElementNS(NS,'text');text.textContent=n.shortLabel;text.setAttribute('text-anchor','middle');text.setAttribute('dominant-baseline','middle');text.setAttribute('x',n.labelX-n.x);text.setAttribute('y',n.labelY-n.y);const title=document.createElementNS(NS,'title');title.textContent=n.title;g.append(c,text,title);nodeLayer.append(g);g.addEventListener('click',ev=>{ev.stopPropagation();selectNode(n,true)});let dragging=false;g.addEventListener('pointerdown',ev=>{dragging=true;g.setPointerCapture(ev.pointerId);ev.stopPropagation()});g.addEventListener('pointermove',ev=>{if(!dragging)return;const p=svgPoint(ev),dx=p.x-n.x,dy=p.y-n.y;n.x=p.x;n.y=p.y;n.labelX+=dx;n.labelY+=dy;renderPositions()});g.addEventListener('pointerup',()=>dragging=false);return {n,g,text}});
function svgPoint(ev){const rect=svg.getBoundingClientRect();return{x:((ev.clientX-rect.left)/rect.width*1200-transform.x)/transform.k,y:((ev.clientY-rect.top)/rect.height*820-transform.y)/transform.k}}function setLinePosition(item){const {e,line}=item;line.setAttribute('x1',e.sourceNode.x);line.setAttribute('y1',e.sourceNode.y);line.setAttribute('x2',e.targetNode.x);line.setAttribute('y2',e.targetNode.y)}function renderPositions(){edgeEls.forEach(setLinePosition);citationEdgeEls.forEach(setLinePosition);nodeEls.forEach(({n,g})=>g.setAttribute('transform',`translate(${n.x} ${n.y})`));viewport.setAttribute('transform',`translate(${transform.x} ${transform.y}) scale(${transform.k})`)}
function visible(n){if(n.roles.includes('seed'))return true;const roleOk=roleFilter==='all'||n.roles.includes(roleFilter);const hay=`${n.title} ${(n.authors||[]).map(a=>a.name).join(' ')}`.toLowerCase();return roleOk&&hay.includes(query)&&Number(n.citationCount||0)>=minCitations}function applyFilters(){nodeEls.forEach(({n,g,text})=>{const on=visible(n);g.style.display=on?'':'none';text.style.display=showLabels?'':'none'});edgeEls.forEach(({e,line})=>line.style.display=showSimilarity&&visible(e.sourceNode)&&visible(e.targetNode)?'':'none');citationEdgeEls.forEach(({e,line})=>line.style.display=showCitations&&visible(e.sourceNode)&&visible(e.targetNode)?'':'none');renderList()}
const esc=value=>String(value??'').replace(/[&<>"']/g,ch=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch]));function paperUrl(n){const id=n.externalIds?.ArXiv;if(id)return `https://arxiv.org/abs/${encodeURIComponent(id)}`;return String(n.url||'').startsWith('https://www.semanticscholar.org/')?n.url:'#'}function roleName(role){return{seed:'起点论文',reference:'关键前作',citation:'后续工作',similar:'语义相似'}[role]||role}function selectNode(n,center=false){selected=n;nodeEls.forEach(x=>x.g.classList.toggle('selected',x.n===n));document.querySelector('#detail-role').textContent=n.roles.map(roleName).join(' · ');document.querySelector('#detail-title').textContent=n.title||'无标题';document.querySelector('#detail-authors').textContent=(n.authors||[]).map(a=>a.name).join(', ')||'作者未知';document.querySelector('#detail-meta').innerHTML=`<span>${esc(n.year||'年份未知')}</span><span>${esc(n.venue||'出版信息未核实')}</span><span>${Number(n.citationCount)||0} Citations</span>`;document.querySelector('#detail-roles').innerHTML=`<div class="role-pills">${n.roles.map(r=>`<i>${esc(roleName(r))}</i>`).join('')}</div>`;const semanticUrl=String(n.url||'').startsWith('https://www.semanticscholar.org/')?n.url:'';document.querySelector('#detail-links').innerHTML=`<a href="${paperUrl(n)}" target="_blank" rel="noreferrer">打开论文</a>${semanticUrl?`<a href="${semanticUrl}" target="_blank" rel="noreferrer">Semantic Scholar</a>`:''}`;document.querySelector('#detail-abstract').textContent=n.abstract||'暂无摘要。';document.querySelectorAll('.paper-list article').forEach(el=>el.classList.toggle('active',el.dataset.id===n.paperId));if(center){transform.x=600-n.x*transform.k;transform.y=410-n.y*transform.k;renderPositions()}}
function renderList(){const list=document.querySelector('#paper-list');const filtered=nodes.filter(visible).sort((a,b)=>(b.citationCount||0)-(a.citationCount||0));list.replaceChildren();if(!filtered.length){list.innerHTML='<div class="empty-list">没有符合筛选条件的论文。</div>';return}for(const n of filtered){const el=document.createElement('article');el.dataset.id=n.paperId;el.innerHTML=`<h3>${esc(n.title||'无标题')}</h3><p>${esc((n.authors||[]).slice(0,3).map(a=>a.name).join(', '))} · ${esc(n.year||'')} · ${Number(n.citationCount)||0} Citations</p><div class="role-pills">${n.roles.map(r=>`<i>${esc(roleName(r))}</i>`).join('')}</div>`;el.addEventListener('click',()=>selectNode(n,true));list.append(el)}selectNode(selected,false)}
document.querySelectorAll('.tabs button').forEach(btn=>btn.addEventListener('click',()=>{roleFilter=btn.dataset.role;document.querySelectorAll('.tabs button').forEach(x=>x.classList.toggle('active',x===btn));applyFilters()}));document.querySelector('#paper-search').addEventListener('input',ev=>{query=ev.target.value.trim().toLowerCase();applyFilters()});document.querySelector('#citation-filter').addEventListener('input',ev=>{minCitations=Number(ev.target.value);document.querySelector('#citation-value').textContent=minCitations;applyFilters()});document.querySelector('#toggle-labels').addEventListener('click',ev=>{showLabels=!showLabels;ev.target.textContent=showLabels?'隐藏标签':'显示全部标签';applyFilters()});document.querySelector('#toggle-similarity').addEventListener('click',ev=>{showSimilarity=!showSimilarity;ev.target.textContent=showSimilarity?'隐藏相似线':'显示相似线';ev.target.classList.toggle('active',showSimilarity);applyFilters()});document.querySelector('#toggle-citations').addEventListener('click',ev=>{showCitations=!showCitations;ev.target.textContent=showCitations?'隐藏引用边':'显示引用边';ev.target.classList.toggle('active',showCitations);applyFilters()});const helpDialog=document.querySelector('#graph-help');document.querySelector('#graph-help-open').addEventListener('click',()=>helpDialog.showModal());document.querySelector('#graph-help-close').addEventListener('click',()=>helpDialog.close());document.querySelector('#reset-view').addEventListener('click',()=>{transform={x:0,y:0,k:1};renderPositions()});document.querySelector('#center-seed').addEventListener('click',()=>selectNode(seed,true));svg.addEventListener('wheel',ev=>{ev.preventDefault();const old=transform.k,next=Math.max(.45,Math.min(3,old*(ev.deltaY<0?1.12:.89))),rect=svg.getBoundingClientRect(),px=(ev.clientX-rect.left)/rect.width*1200,py=(ev.clientY-rect.top)/rect.height*820;transform.x=px-(px-transform.x)*next/old;transform.y=py-(py-transform.y)*next/old;transform.k=next;renderPositions()},{passive:false});let panning=false,last={x:0,y:0};svg.addEventListener('pointerdown',ev=>{if(ev.target===svg){panning=true;last={x:ev.clientX,y:ev.clientY};svg.setPointerCapture(ev.pointerId)}});svg.addEventListener('pointermove',ev=>{if(!panning)return;const rect=svg.getBoundingClientRect();transform.x+=(ev.clientX-last.x)/rect.width*1200;transform.y+=(ev.clientY-last.y)/rect.height*820;last={x:ev.clientX,y:ev.clientY};renderPositions()});svg.addEventListener('pointerup',()=>panning=false);renderPositions();applyFilters();selectNode(seed,false);
</script></body></html>"""
        return page.replace("__TITLE__", title).replace("__GRAPH_DATA__", graph_json)
