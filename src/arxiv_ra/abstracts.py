from __future__ import annotations

import hashlib
import re
import threading
from pathlib import Path
from typing import Any

from .config import AppConfig
from .llm import LLMClient
from .utils import extract_json_object, normalize_space, read_json, write_json


_CACHE_LOCK = threading.Lock()
_CACHE_VERSION = 2


def _cache_key(config: AppConfig, arxiv_id: str) -> str:
    return f"{config.profile_id or 'default'}::{arxiv_id}"


def _signature(config: AppConfig, paper: dict[str, Any]) -> str:
    source = "\n".join(
        [
            str(_CACHE_VERSION),
            config.profile_id,
            config.profile_name,
            config.discovery.interest_description,
            "|".join(config.discovery.positive_keywords),
            str(paper.get("title", "")),
            str(paper.get("abstract", "")),
        ]
    )
    return hashlib.sha256(source.encode("utf-8")).hexdigest()


def _report_section(output_root: Path, arxiv_id: str, heading: str) -> str:
    candidates = sorted(
        output_root.glob(
            f"????-??-??/reports/{arxiv_id.replace('/', '-')}-*/report.md"
        ),
        reverse=True,
    )
    for path in candidates:
        markdown = path.read_text(encoding="utf-8")
        match = re.search(
            rf"(?ms)^##\s+{re.escape(heading)}\s*\n(.*?)(?=^##\s+|\Z)",
            markdown,
        )
        if match:
            summary = normalize_space(
                re.sub(r"(?m)^\s*[-*]\s+", "", re.sub(r"[*_`>]", "", match.group(1)))
            )
            if summary:
                return summary
    return ""


def _clip(value: str, maximum: int) -> str:
    value = normalize_space(value)
    if len(value) <= maximum:
        return value
    prefix = value[:maximum]
    cut = max(prefix.rfind(mark) for mark in "。！？；")
    if cut >= maximum // 2:
        return prefix[: cut + 1]
    return prefix.rstrip("，、：; ") + "…"


def localize_abstracts(
    config: AppConfig,
    output_root: Path,
    recommendations: list[dict[str, Any]],
    generate: bool = True,
) -> None:
    """Attach cached, polished Chinese abstracts to recommendation paper payloads."""
    if not recommendations:
        return
    cache_path = output_root / "abstract-translations.json"
    with _CACHE_LOCK:
        cache = read_json(cache_path, {}) or {}
    missing: list[dict[str, Any]] = []
    for item in recommendations:
        paper = item.get("paper") or {}
        arxiv_id = str(paper.get("arxiv_id") or "")
        cache_key = _cache_key(config, arxiv_id)
        cached = cache.get(cache_key) or {}
        if (
            cached.get("version") == _CACHE_VERSION
            and cached.get("signature") == _signature(config, paper)
            and cached.get("abstract_zh")
            and cached.get("recommendation_detail")
        ):
            paper["abstract_zh"] = str(cached["abstract_zh"])
            paper["recommendation_detail"] = str(
                cached["recommendation_detail"]
            )
        elif (
            paper.get("abstract")
            or _report_section(output_root, arxiv_id, "一句话总结")
            or _report_section(output_root, arxiv_id, "为什么值得阅读")
        ):
            missing.append(paper)

    translated: dict[str, dict[str, str]] = {}
    llm_missing = [paper for paper in missing if paper.get("abstract")]
    if generate and llm_missing:
        llm = LLMClient(config.llm)
        if llm.enabled:
            source = "\n\n".join(
                f"ID: {paper.get('arxiv_id', '')}\n标题: {paper.get('title', '')}\n已有短理由: {paper.get('recommendation_reason') or '无'}\n英文摘要: {str(paper.get('abstract') or '')[:2600]}"
                for paper in llm_missing
            )
            try:
                raw = llm.chat(
                    "你是严谨的 AI 论文阅读编辑。只依据提供的标题、摘要和研究兴趣写作，不得夸大贡献或补写结果。",
                    f"""研究方向：{config.profile_name}
研究兴趣：{config.discovery.interest_description or '未单独描述'}
重点关键词：{', '.join(config.discovery.positive_keywords) or '未配置'}

请为以下论文分别生成“摘要精要”和“推荐依据”。

{source}

要求：
1. `abstract_zh`：2-3 句、120-220 字，不逐句翻译；依次交代研究问题、核心方法和摘要中明确给出的关键结果。专有名词、指标和重要数值不得丢失。
2. `recommendation_detail`：2-3 句、100-180 字；明确说明它与上述研究方向的连接点、最值得关注的具体机制/证据，以及可能带来的研究启发。禁止使用“主题相关、值得阅读、具有重要意义”等空泛表述。
3. 摘要没有提供数值或局限时不要自行补充。
4. 只返回 JSON：{{"papers":[{{"id":"arXiv ID","abstract_zh":"摘要精要","recommendation_detail":"推荐依据"}}]}}。""",
                    json_mode=True,
                )
                payload = extract_json_object(raw)
                translated = {
                    str(item.get("id") or ""): {
                        "abstract_zh": _clip(str(item.get("abstract_zh") or ""), 220),
                        "recommendation_detail": _clip(
                            str(item.get("recommendation_detail") or ""), 180
                        ),
                    }
                    for item in payload.get("papers", [])
                    if item.get("id")
                    and item.get("abstract_zh")
                    and item.get("recommendation_detail")
                }
            except Exception:
                translated = {}

    cache_updates: dict[str, dict[str, Any]] = {}
    for paper in missing:
        arxiv_id = str(paper.get("arxiv_id") or "")
        generated = translated.get(arxiv_id) or {}
        abstract_zh = str(generated.get("abstract_zh") or "") or _clip(
            _report_section(output_root, arxiv_id, "一句话总结"), 220
        )
        if not abstract_zh:
            abstract_zh = _clip(
                str(
                    paper.get("recommendation_reason")
                    or paper.get("abstract")
                    or ""
                ),
                220,
            )
        recommendation_detail = str(
            generated.get("recommendation_detail") or ""
        ) or _clip(_report_section(output_root, arxiv_id, "为什么值得阅读"), 180)
        if not recommendation_detail:
            recommendation_detail = _clip(
                str(paper.get("recommendation_reason") or abstract_zh), 180
            )
        paper["abstract_zh"] = abstract_zh
        paper["recommendation_detail"] = recommendation_detail
        if generate:
            cache_updates[_cache_key(config, arxiv_id)] = {
                "version": _CACHE_VERSION,
                "signature": _signature(config, paper),
                "abstract_zh": abstract_zh,
                "recommendation_detail": recommendation_detail,
                "source": "llm-localization" if arxiv_id in translated else "fallback",
            }
    if cache_updates:
        with _CACHE_LOCK:
            latest_cache = read_json(cache_path, {}) or {}
            latest_cache.update(cache_updates)
            write_json(cache_path, latest_cache)
