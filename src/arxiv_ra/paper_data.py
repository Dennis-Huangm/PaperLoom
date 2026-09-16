from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import re
import time

from .library import PaperLibraryStore
from .models import Paper
from .storage import read_recommendations
from .utils import read_json, write_json


def alpha_enabled(config) -> bool:
    return config.provider in {"auto", "hybrid"} and config.alphaxiv_fallback_enabled


def base_id(value: str) -> str:
    return re.sub(r"v\d+$", "", value.strip(), flags=re.I)


def requested_version(value: str) -> int | None:
    match = re.search(r"v(\d+)$", value, re.I)
    return int(match[1]) if match else None


def with_sources(paper: Paper, sources: list[str]) -> Paper:
    return replace(paper, discovery_sources=list(dict.fromkeys(sources)))


def versioned(paper: Paper) -> Paper:
    if not paper.version:
        return paper
    target = f"{paper.arxiv_id}v{paper.version}"
    return replace(paper, abs_url=f"https://arxiv.org/abs/{target}", pdf_url=f"https://arxiv.org/pdf/{target}")


def matches(paper: Paper, arxiv_id: str) -> bool:
    version = requested_version(arxiv_id)
    return paper.arxiv_id == base_id(arxiv_id) and (version is None or paper.version == version)


def local_paper_item(output_root: Path, profile_id: str, arxiv_id: str,
                     *, source_date: str = "", origin: str = "", latest: bool = False) -> dict | None:
    """Locate one stored snapshot without mixing it with newer network metadata."""
    def eligible(item):
        raw = item.get("paper") or {}
        return raw.get("arxiv_id") == base_id(arxiv_id) and (
            requested_version(arxiv_id) is None or raw.get("version") == requested_version(arxiv_id))

    candidates = []

    if source_date and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", source_date):
        raise ValueError("日期格式必须为 YYYY-MM-DD")
    if origin != "library":
        dates = [output_root / source_date] if source_date else sorted(output_root.glob("????-??-??"), reverse=True)
        for date_dir in dates:
            for item in read_recommendations(output_root, date_dir.name, profile_id):
                if eligible(item):
                    candidate = {**item, "_source_date": date_dir.name}
                    if not latest:
                        return candidate
                    candidates.append(candidate)
        if source_date or origin == "recommendation":
            return candidates[0] if candidates else None
    saved = PaperLibraryStore(output_root, profile_id).all().get(base_id(arxiv_id))
    if saved and eligible(saved):
        candidate = {"paper": saved["paper"], "verified": saved.get("verified") or {},
                     "_source_date": saved.get("source_date", "")}
        if not latest:
            return candidate
        candidates.append(candidate)
    if origin == "library":
        return candidates[0] if candidates else None
    for path in sorted(output_root.glob("????-??-??/reports/*/metadata.json"), reverse=True):
        try:
            payload = read_json(path, {}) or {}
        except (ValueError, OSError):
            continue
        if not isinstance(payload, dict):
            continue
        if payload.get("profile_id") not in (None, "", profile_id):
            continue
        if eligible(payload):
            if not latest:
                return payload
            candidates.append(payload)
    ranked = []
    for item in candidates:
        try:
            paper = Paper.from_dict(item["paper"])
        except (ValueError, TypeError, KeyError):
            continue
        rank = (paper.metadata_status == "complete", paper.version or 0,
                paper.updated.timestamp() if paper.updated else 0)
        ranked.append((rank, item))
    return max(ranked, key=lambda pair: pair[0])[1] if ranked else None


class PaperResolver:
    """Select complete, identity-matching data according to the reading intent."""

    def __init__(self, config, arxiv, alphaxiv, output_root: Path | None = None):
        self.config, self.arxiv, self.alphaxiv = config, arxiv, alphaxiv
        self.cache_root = output_root / "metadata-cache" if output_root else None

    def _path(self, arxiv_id: str) -> Path | None:
        if not re.fullmatch(r"(?:[a-z-]+(?:\.[a-z]{2})?/\d{7}|\d{4}\.\d{4,5})(?:v[1-9]\d*)?", arxiv_id, re.I):
            raise ValueError("无效的 arXiv ID")
        return self.cache_root / f"{arxiv_id.replace('/', '_')}.json" if self.cache_root else None

    def cached(self, arxiv_id: str, *, fresh_only: bool = False) -> Paper | None:
        path = self._path(arxiv_id)
        try:
            if path and fresh_only and path.exists() and time.time() - path.stat().st_mtime > 86400:
                return None
            data = read_json(path, {}) if path else {}
            paper = Paper.from_dict(data) if data else None
        except (ValueError, TypeError, OSError, KeyError):
            return None
        if paper and matches(paper, arxiv_id) and paper.metadata_status == "complete":
            return versioned(paper)
        return None

    def remember(self, paper: Paper, requested_id: str | None = None) -> None:
        target = requested_id or paper.arxiv_id
        if not matches(paper, target):
            raise ValueError("论文缓存的 ID 或版本不匹配")
        if paper.metadata_status != "complete":
            return
        paper = versioned(paper)
        if paper.version:
            path = self._path(f"{paper.arxiv_id}v{paper.version}")
            if path:
                write_json(path, paper.to_dict())
        path = self._path(target)
        previous = self.cached(target)
        if path and (not previous or (previous.version or 0) <= (paper.version or 0)):
            write_json(path, paper.to_dict())

    def best_available(self, arxiv_id: str, *papers: Paper | None) -> Paper | None:
        candidates = [p for p in (*papers, self.cached(arxiv_id), self.cached(base_id(arxiv_id)))
                      if p and matches(p, arxiv_id)]
        return max(candidates, key=lambda p: (p.metadata_status == "complete", p.version or 0,
                                            p.updated.timestamp() if p.updated else 0), default=None)

    def resolve(self, arxiv_id: str, snapshot: Paper | None = None, *, intent: str = "snapshot") -> Paper:
        self._path(arxiv_id)
        if intent not in {"snapshot", "latest"}:
            raise ValueError("无效的论文读取意图")
        if snapshot and not matches(snapshot, arxiv_id):
            snapshot = None
        target = arxiv_id
        if intent == "snapshot" and snapshot and snapshot.version and requested_version(target) is None:
            target = f"{snapshot.arxiv_id}v{snapshot.version}"
        if intent == "snapshot" and snapshot and snapshot.metadata_status == "complete":
            return versioned(snapshot)
        cached = self.cached(target)
        if cached and (intent == "snapshot" or requested_version(target) is not None):
            return with_sources(cached, snapshot.discovery_sources if snapshot else cached.discovery_sources)
        try:
            paper = self.arxiv.get(target)
            if not matches(paper, target):
                raise LookupError("arXiv 返回的论文 ID 或版本不匹配")
        except Exception as arxiv_error:
            fallback = self.best_available(target, snapshot)
            if fallback:
                # Exact revisions remain usable; a latest request must reveal stale fallback.
                if intent == "latest" and requested_version(target) is None:
                    fallback = replace(fallback, resolution_note="arXiv 暂时不可用，使用本地数据；尚未确认是否为最新版本")
                return versioned(fallback)
            if self.config and alpha_enabled(self.config) and self.alphaxiv and self.alphaxiv.enabled:
                try:
                    paper = self.alphaxiv.lookup(target)
                    if paper and matches(paper, target):
                        return versioned(replace(paper, resolution_note="arXiv 暂时不可用，使用 alphaXiv 数据；版本信息以实际解析结果为准"))
                except Exception:
                    pass
            raise RuntimeError(f"arXiv 无法获取 {target}，本地无匹配版本，alphaXiv 未能精确解析该 ID 和版本") from arxiv_error
        if snapshot:
            paper = with_sources(paper, snapshot.discovery_sources)
        self.remember(paper, target)
        return versioned(paper)
