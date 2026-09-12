from __future__ import annotations

import re
import shutil
import uuid
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

from .arxiv_client import ArxivClient
from .config import AppConfig
from .llm import LLMClient
from .utils import atomic_write_text, extract_json_object


PROFILE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
ARXIV_CATEGORY_RE = re.compile(r"^[a-z-]+(?:\.[A-Za-z-]+)?$")


def _atomic_yaml(path: Path, payload: dict[str, Any]) -> None:
    atomic_write_text(
        path,
        yaml.safe_dump(payload, allow_unicode=True, sort_keys=False),
    )


def _slug(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.casefold()).strip("-")
    return slug[:48] or f"profile-{uuid.uuid4().hex[:8]}"


class ProfileManager:
    def __init__(self, project_root: Path) -> None:
        self.root = project_root / "profiles"
        self.active_path = self.root / "active.txt"

    def ensure_default(self, config_path: Path) -> str:
        self.root.mkdir(parents=True, exist_ok=True)
        existing = list(self.root.glob("*.yaml"))
        if existing:
            if not self.active_id():
                self.activate(existing[0].stem)
            return self.active_id()
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
        description = str((raw.get("discovery") or {}).get("interest_description") or "")
        name = "AgenticT2I" if "agentict2i" in description.casefold() else "默认研究方向"
        profile_id = "agentict2i" if name == "AgenticT2I" else "default"
        payload = {
            "id": profile_id,
            "name": name,
            "description": description,
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "source": {
                "keywords": (raw.get("discovery") or {}).get("positive_keywords") or [],
                "negative_keywords": (raw.get("discovery") or {}).get("negative_keywords") or [],
                "reference_papers": [
                    {"arxiv_id": item, "title": ""}
                    for item in ((raw.get("discovery") or {}).get("seed_papers") or [])
                ],
            },
            "discovery": raw.get("discovery") or {},
            "ranking": raw.get("ranking") or {},
        }
        self.save(payload)
        self.activate(profile_id)
        output_dir = Path(str(raw.get("output_dir") or "run"))
        output_root = output_dir if output_dir.is_absolute() else config_path.parent / output_dir
        legacy_state = output_root / "state.json"
        profile_state = output_root / f"state-{profile_id}.json"
        if legacy_state.exists() and not profile_state.exists():
            profile_state.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(legacy_state, profile_state)
        return profile_id

    def active_id(self) -> str:
        if not self.active_path.exists():
            return ""
        return self.active_path.read_text(encoding="utf-8").strip()

    def activate(self, profile_id: str) -> None:
        if not PROFILE_ID_RE.match(profile_id) or not (self.root / f"{profile_id}.yaml").exists():
            raise ValueError("研究方向档案不存在")
        self.root.mkdir(parents=True, exist_ok=True)
        atomic_write_text(self.active_path, profile_id + "\n")

    def get(self, profile_id: str) -> dict[str, Any]:
        if not PROFILE_ID_RE.match(profile_id):
            raise ValueError("无效的研究方向 ID")
        path = self.root / f"{profile_id}.yaml"
        if not path.exists():
            raise KeyError(profile_id)
        return yaml.safe_load(path.read_text(encoding="utf-8")) or {}

    def list(self) -> list[dict[str, Any]]:
        active = self.active_id()
        profiles: list[dict[str, Any]] = []
        if not self.root.exists():
            return profiles
        for path in sorted(self.root.glob("*.yaml")):
            payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            discovery = payload.get("discovery") or {}
            profiles.append(
                {
                    "id": path.stem,
                    "name": payload.get("name") or path.stem,
                    "description": payload.get("description") or discovery.get("interest_description", ""),
                    "keywords": discovery.get("positive_keywords") or [],
                    "references": (payload.get("source") or {}).get("reference_papers") or [],
                    "recommendation_count": discovery.get("recommendation_count", 0),
                    "lookback_days": discovery.get("lookback_days", 0),
                    "active": path.stem == active,
                    "path": str(path),
                }
            )
        profiles.sort(key=lambda item: (not item["active"], item["name"].casefold()))
        return profiles

    def save(self, payload: dict[str, Any]) -> Path:
        profile_id = str(payload.get("id") or "")
        if not PROFILE_ID_RE.match(profile_id):
            raise ValueError("无效的研究方向 ID")
        path = self.root / f"{profile_id}.yaml"
        _atomic_yaml(path, payload)
        return path

    def update_active_search(self, discovery: dict[str, Any], ranking: dict[str, Any]) -> None:
        active = self.active_id()
        if not active:
            return
        payload = self.get(active)
        payload["discovery"] = discovery
        payload["ranking"] = ranking
        payload["updated_at"] = datetime.now().isoformat(timespec="seconds")
        self.save(payload)

    def unique_id(self, name: str) -> str:
        base = _slug(name)
        candidate = base
        counter = 2
        while (self.root / f"{candidate}.yaml").exists():
            candidate = f"{base[:54]}-{counter}"
            counter += 1
        return candidate


class ProfileGenerator:
    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self.arxiv = ArxivClient()
        self.llm = LLMClient(config.llm)

    def generate(
        self,
        profile_id: str,
        name: str,
        keywords: list[str],
        negative_keywords: list[str],
        reference_ids: list[str],
        description: str = "",
        recommendation_count: int | None = None,
    ) -> dict[str, Any]:
        references = []
        for arxiv_id in reference_ids[:12]:
            paper = self.arxiv.get(arxiv_id)
            references.append(
                {
                    "arxiv_id": paper.arxiv_id,
                    "title": paper.title,
                    "abstract": paper.abstract,
                    "categories": paper.categories,
                }
            )
        generated = self._llm_profile(name, description, keywords, negative_keywords, references)
        discovery = asdict(self.config.discovery)
        discovery.update(
            {
                "interest_description": generated.get("interest_description")
                or description
                or f"{name}：关注 {', '.join(keywords)}。",
                "arxiv_categories": self._categories(generated, references),
                "arxiv_query_terms": self._strings(generated.get("arxiv_query_terms"), keywords, 16),
                "positive_keywords": self._strings(generated.get("positive_keywords"), keywords, 40),
                "negative_keywords": self._strings(
                    generated.get("negative_keywords"), negative_keywords, 30
                ),
                "seed_papers": [item["arxiv_id"] for item in references],
                "concept_groups": self._groups(generated.get("concept_groups"), keywords),
            }
        )
        try:
            minimum_groups = int(
                generated.get("minimum_concept_groups", len(discovery["concept_groups"]))
            )
        except (TypeError, ValueError):
            minimum_groups = len(discovery["concept_groups"])
        discovery["minimum_concept_groups"] = max(
            0, min(minimum_groups, len(discovery["concept_groups"]))
        )
        if recommendation_count is not None:
            discovery["recommendation_count"] = recommendation_count
        return {
            "id": profile_id,
            "name": name,
            "description": discovery["interest_description"],
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "source": {
                "keywords": keywords,
                "negative_keywords": negative_keywords,
                "reference_papers": [
                    {"arxiv_id": item["arxiv_id"], "title": item["title"]}
                    for item in references
                ],
            },
            "discovery": discovery,
            "ranking": asdict(self.config.ranking),
        }

    def _llm_profile(
        self,
        name: str,
        description: str,
        keywords: list[str],
        negative_keywords: list[str],
        references: list[dict[str, Any]],
    ) -> dict[str, Any]:
        if not self.llm.enabled:
            return {}
        reference_text = "\n\n".join(
            f"arXiv:{item['arxiv_id']}\n标题：{item['title']}\n类别：{', '.join(item['categories'])}\n摘要：{item['abstract'][:1800]}"
            for item in references
        ) or "未提供参考论文"
        raw = self.llm.chat(
            "你是严谨的 AI 研究文献检索配置专家。只根据用户关键词和参考论文归纳研究方向，不得扩展成泛化的 AI 主题。",
            f"""方向名称：{name}
用户描述：{description or '未提供'}
重点关键词：{', '.join(keywords)}
排除关键词：{', '.join(negative_keywords) or '无'}

参考论文：
{reference_text}

请生成适合 arXiv 每日检索的配置，只返回 JSON 对象：
{{
  "interest_description":"2-4句清晰研究兴趣描述",
  "arxiv_categories":["cs.CV"],
  "arxiv_query_terms":["服务端检索短语"],
  "positive_keywords":["本地排序关键词"],
  "negative_keywords":["应降低优先级的方向"],
  "concept_groups":[["同义词组1"],["同义词组2"]],
  "minimum_concept_groups":2
}}

要求：查询词和关键词必须具体；concept_groups 表示论文必须同时命中的研究轴线；不要把 LLM、AI、deep learning 单独作为宽泛检索词。""",
            json_mode=True,
        )
        payload = extract_json_object(raw)
        return payload if isinstance(payload, dict) else {}

    @staticmethod
    def _strings(value: Any, fallback: list[str], limit: int) -> list[str]:
        items = value if isinstance(value, list) else fallback
        result: list[str] = []
        for item in items:
            text = str(item).strip()
            if text and text.casefold() not in {entry.casefold() for entry in result}:
                result.append(text)
        return result[:limit]

    def _categories(
        self, generated: dict[str, Any], references: list[dict[str, Any]]
    ) -> list[str]:
        values = generated.get("arxiv_categories") or [
            category for item in references for category in item["categories"]
        ]
        categories = [str(item) for item in values if ARXIV_CATEGORY_RE.match(str(item))]
        return list(dict.fromkeys(categories))[:12] or list(self.config.discovery.arxiv_categories)

    @staticmethod
    def _groups(value: Any, keywords: list[str]) -> list[list[str]]:
        if isinstance(value, list):
            groups = []
            for group in value:
                if isinstance(group, list):
                    terms = [str(term).strip() for term in group if str(term).strip()]
                    if terms:
                        groups.append(terms[:16])
            if groups:
                return groups[:6]
        return [keywords[:16]] if keywords else []
