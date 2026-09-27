"""Rebuildable, local keyword index. Original files remain the source of truth."""
from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import unicodedata
import uuid
from urllib.parse import urlencode

import pymupdf

from .reading_state import ReadingStateStore, _locked
from .report_store import profile_matches
from .storage import read_recommendations
from .task_runtime import task_checkpoint, task_progress, task_warning
from .utils import read_json
from .web_catalog import artifact_url


KINDS = {"metadata": "标题与摘要", "notes": "个人笔记与标签", "report": "报告正文", "pdf": "PDF 原文"}
QUALITIES = {"full": "全文报告", "abstract": "摘要级报告", "unknown": "质量未记录", "none": "不适用"}


def normalize(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


def digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def query_terms(query: str) -> list[str]:
    if len(query) > 200:
        raise ValueError("搜索词最多 200 字")
    if any(unicodedata.category(char) == "Cc" and not char.isspace() for char in query):
        raise ValueError("搜索词不能包含控制字符")
    terms = list(dict.fromkeys(normalize(query).split()))
    if len(terms) > 12:
        raise ValueError("一次最多输入 12 个关键词")
    return terms


@dataclass
class Source:
    key: str
    signature: str
    title: str
    aid: str
    version: int | None
    kind: str
    quality: str = "none"
    text: str = ""
    path: Path | None = None
    url: str = ""


class SearchIndex:
    def __init__(self, root: Path, profile: str):
        self.root = root.resolve()
        self.profile = profile
        # Hash profile names: callers cannot use them as filesystem paths.
        self.path = self.root / ".search" / f"{digest(profile)[:24]}.sqlite3"
        self._contained(self.path)

    def _contained(self, path: Path) -> Path:
        path.resolve().relative_to(self.root)
        return path

    def _signature(self, *paths: Path) -> str:
        return digest([(p.relative_to(self.root).as_posix(), self._contained(p).stat().st_size,
                        p.stat().st_mtime_ns) for p in paths])

    def sources(self) -> tuple[list[Source], list[str]]:
        sources: dict[str, Source] = {}
        warnings: list[str] = []

        def metadata(paper, key, url="", text=None, kind="metadata", version=None):
            if not isinstance(paper, dict) or not paper.get("arxiv_id"):
                return
            aid = str(paper["arxiv_id"])
            title = str(paper.get("title") or aid)
            revision = version if kind == "notes" else paper.get("version")
            revision = revision if isinstance(revision, int) and revision > 0 else None
            if text is None:
                text = "\n\n".join(str(paper.get(field) or "") for field in ("abstract", "abstract_zh"))
            value = [title, aid, revision, text, url]
            sources[key] = Source(key, digest(value), title, aid, revision, kind, text=text, url=url)

        store = ReadingStateStore(self.root, self.profile)
        # A broken reading-state file must abort refresh, not silently erase notes.
        for path in (store.path, store.library_path, store.feedback_path, store.lock_path):
            self._contained(path)
        state = store.snapshot()
        papers = {aid: entry.get("paper", {}) for aid, entry in state["library"].items()}
        for event in reversed(state.get("activity", [])):
            paper = event.get("paper") or {}
            if paper.get("arxiv_id") not in papers:
                papers[paper.get("arxiv_id")] = paper
        for aid, entry in state["library"].items():
            metadata(entry.get("paper"), f"library:{aid}", "/library?" + urlencode({"q": aid}))
        for aid, record in state.get("reading", {}).items():
            text = str(record.get("notes") or "") + "\n\n标签：" + ", ".join(record.get("tags") or [])
            if record.get("notes") or record.get("tags"):
                metadata(papers.get(aid) or {"arxiv_id": aid}, f"notes:{aid}",
                         "/library?" + urlencode({"q": aid}) if aid in state["library"] else "",
                         text=text, kind="notes", version=record.get("read_version"))

        for folder in sorted(self.root.glob("????-??-??"), reverse=True):
            task_checkpoint()
            self._contained(folder)
            for path in (folder / f"recommendations-{self.profile}.json", folder / "recommendations.json"):
                self._contained(path)
            for item in read_recommendations(self.root, folder.name, self.profile):
                paper = item.get("paper") or {}
                key = f"recommendation:{paper.get('arxiv_id')}:{paper.get('version')}"
                if key not in sources:
                    metadata(paper, key, "/?" + urlencode({"date": folder.name}))

        paths = list(self.root.glob("????-??-??/reports/*/metadata.json"))
        paths += list((self.root / "papers" / (self.profile or "default")).glob("*/v*/metadata.json"))
        for path in paths:
            task_checkpoint()
            try:
                payload = read_json(self._contained(path), {})
                if not isinstance(payload, dict) or not profile_matches(payload, self.profile):
                    continue
                paper = payload.get("paper") or {}
                if not isinstance(paper, dict) or not paper.get("arxiv_id"):
                    continue
                key = path.relative_to(self.root).as_posix()
                origin = next((path.with_name(name) for name in ("report.html", "paper.pdf")
                               if path.with_name(name).is_file()), None)
                metadata(paper, key + ":metadata", (artifact_url(origin, self.root) or "") if origin else "")
                quality = payload.get("report_quality", "unknown") if "reports" in path.parts else "none"
                if quality not in QUALITIES:
                    quality = "unknown"
                for name, kind in (("report.md", "report"), ("paper.pdf", "pdf")):
                    body = path.with_name(name)
                    if not body.is_file():
                        continue
                    self._contained(body)
                    url_path = body.with_suffix(".html") if kind == "report" else body
                    url = artifact_url(url_path, self.root) if url_path.is_file() else ""
                    source_key = body.relative_to(self.root).as_posix()
                    sources[source_key] = Source(source_key, self._signature(path, body),
                        str(paper.get("title") or paper["arxiv_id"]), str(paper["arxiv_id"]),
                        paper.get("version") if isinstance(paper.get("version"), int) else None,
                        kind, quality, path=body, url=url or "")
            except (OSError, ValueError, TypeError):
                warnings.append(f"跳过无法读取或不在输出目录内的材料：{path.name}（{path.parent.name}）")
        return list(sources.values()), warnings

    @staticmethod
    def _chunks(text: str):
        # Preserve exact characters and overlap long paragraphs at boundaries.
        for start in range(0, len(text), 1800):
            yield text[start:start + 2000]

    def _documents(self, source: Source, warnings: list[str]):
        if source.kind == "pdf":
            with pymupdf.open(source.path) as pdf:
                empty = 0
                for page_number, page in enumerate(pdf, 1):
                    task_checkpoint()
                    text = page.get_text(sort=True)
                    if not text.strip():
                        empty += 1
                    for chunk in self._chunks(text):
                        yield page_number, chunk
                if empty:
                    warnings.append(f"{source.title}：{empty}/{len(pdf)} 页无可提取文字，未执行 OCR")
        else:
            text = source.path.read_text(encoding="utf-8") if source.path else source.text
            for chunk in self._chunks(text) if text else [""]:
                yield None, chunk

    def refresh(self, *, rebuild: bool = False) -> dict:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with _locked(self.path.with_suffix(".lock")):
            if rebuild:
                temporary = SearchIndex(self.root, self.profile)
                temporary.path = self.path.with_name(f"rebuild-{uuid.uuid4().hex}.sqlite3")
                try:
                    result = temporary.refresh()
                    task_checkpoint()
                    temporary.path.replace(self.path)
                    return result
                finally:
                    for path in (temporary.path, temporary.path.with_suffix(".lock"),
                                 Path(str(temporary.path) + "-wal"), Path(str(temporary.path) + "-shm")):
                        path.unlink(missing_ok=True)
            sources, warnings = self.sources()
            with closing(sqlite3.connect(self.path, timeout=30)) as db, db:
                db.executescript("""
                    CREATE TABLE IF NOT EXISTS sources (key TEXT PRIMARY KEY, signature TEXT, warnings TEXT);
                    CREATE TABLE IF NOT EXISTS docs (id TEXT UNIQUE, source TEXT, title TEXT, aid TEXT,
                        version INTEGER, kind TEXT, quality TEXT, page INTEGER, body TEXT, url TEXT, normalized TEXT);
                    CREATE INDEX IF NOT EXISTS docs_source ON docs(source);
                    CREATE TABLE IF NOT EXISTS status (key TEXT PRIMARY KEY, value TEXT);
                    CREATE VIRTUAL TABLE IF NOT EXISTS terms USING fts5(normalized, tokenize='trigram');
                """)
                db.execute("BEGIN IMMEDIATE")
                previous = {row[0]: (row[1], json.loads(row[2])) for row in db.execute("SELECT * FROM sources")}
                active = {source.key for source in sources}
                changed = 0

                def remove(key):
                    db.execute("DELETE FROM terms WHERE rowid IN (SELECT rowid FROM docs WHERE source=?)", (key,))
                    db.execute("DELETE FROM docs WHERE source=?", (key,))
                    db.execute("DELETE FROM sources WHERE key=?", (key,))

                for key in previous.keys() - active:
                    remove(key)
                for position, source in enumerate(sources):
                    task_progress(f"更新搜索索引 · {position + 1}/{len(sources)} · {KINDS[source.kind]}",
                                  int(position * 95 / max(1, len(sources))))
                    old = previous.get(source.key)
                    if not rebuild and old and old[0] == source.signature:
                        warnings.extend(old[1])
                        continue
                    remove(source.key)
                    local_warnings: list[str] = []
                    try:
                        for ordinal, (page, body) in enumerate(self._documents(source, local_warnings)):
                            task_checkpoint()
                            revision_id = f"{source.aid}v{source.version}" if source.version else source.aid
                            normalized = normalize(f"{source.title}\n{revision_id}\n{body}")
                            cursor = db.execute("INSERT INTO docs VALUES (?,?,?,?,?,?,?,?,?,?,?)", (
                                digest([source.key, ordinal]), source.key, source.title, source.aid,
                                source.version, source.kind, source.quality, page, body, source.url, normalized))
                            db.execute("INSERT INTO terms(rowid,normalized) VALUES (?,?)", (cursor.lastrowid, normalized))
                        if source.path and self._signature(source.path.with_name("metadata.json"), source.path) != source.signature:
                            raise ValueError("材料在提取期间发生变化")
                        db.execute("INSERT INTO sources VALUES (?,?,?)", (source.key, source.signature, json.dumps(local_warnings)))
                    except (OSError, ValueError, RuntimeError) as exc:
                        remove(source.key)
                        local_warnings.append(f"{source.title}：{KINDS[source.kind]}无法读取（{type(exc).__name__}），已跳过")
                    warnings.extend(local_warnings)
                    changed += 1
                task_checkpoint()
                status = {"updated_at": datetime.now(timezone.utc).isoformat(), "changed": changed,
                          "sources": db.execute("SELECT COUNT(*) FROM sources").fetchone()[0],
                          "documents": db.execute("SELECT COUNT(*) FROM docs").fetchone()[0],
                          "warnings": warnings}
                db.execute("INSERT OR REPLACE INTO status VALUES ('summary',?)", (json.dumps(status),))
            for warning in warnings:
                task_warning("搜索索引", warning)
            return status

    def _read(self):
        db = sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True, timeout=5)
        db.row_factory = sqlite3.Row
        return db

    def status(self) -> dict:
        if not self.path.exists():
            return {}
        try:
            with closing(self._read()) as db:
                row = db.execute("SELECT value FROM status WHERE key='summary'").fetchone()
                return json.loads(row[0]) if row else {}
        except (sqlite3.DatabaseError, ValueError):
            return {"error": "搜索索引无法读取，请重建索引。原始资料不受影响。"}

    def search(self, query: str, *, kind="", quality="", version: int | None = None, page=1) -> dict:
        terms = query_terms(query)
        if (kind and kind not in KINDS) or (quality and quality not in QUALITIES):
            raise ValueError("无效的搜索筛选条件")
        if version is not None and version < 1:
            raise ValueError("修订版必须大于零")
        page = max(1, page)
        result = {"items": [], "total": 0, "page": page, "pages": 0}
        status = self.status()
        if not terms or not status or status.get("error"):
            return result
        clauses, args = [], []
        long_terms = [term for term in terms if len(term) >= 3]
        if long_terms:
            clauses.append("d.rowid IN (SELECT rowid FROM terms WHERE terms MATCH ?)")
            args.append(" AND ".join('"' + term.replace('"', '""') + '"' for term in long_terms))
        for term in terms:
            clauses.append("instr(d.normalized,?)>0")
            args.append(term)
        for column, value in (("kind", kind), ("quality", quality), ("version", version)):
            if value not in (None, ""):
                clauses.append(f"d.{column}=?")
                args.append(value)
        where = " AND ".join(clauses)
        with closing(self._read()) as db:
            # One result per source; avoid flooding the page with every matching PDF chunk.
            grouped = f"SELECT MIN(d.rowid) AS hit FROM docs d WHERE {where} GROUP BY d.source"
            total = db.execute(f"SELECT COUNT(*) FROM ({grouped})", args).fetchone()[0]
            pages = (total + 19) // 20
            page = min(page, max(1, pages))
            rows = db.execute(f"SELECT * FROM docs WHERE rowid IN ({grouped}) ORDER BY title,aid,version DESC,source LIMIT 20 OFFSET ?",
                              [*args, (page - 1) * 20]).fetchall()
        items = []
        for row in rows:
            item = dict(row)
            item["snippet"] = snippet(item["body"], terms)
            items.append(item)
        return {"items": items, "total": total, "page": page, "pages": pages}

    def document(self, doc_id: str) -> dict | None:
        if not self.path.exists():
            return None
        try:
            with closing(self._read()) as db:
                row = db.execute("SELECT d.*,s.signature FROM docs d JOIN sources s ON s.key=d.source WHERE d.id=?", (doc_id,)).fetchone()
        except sqlite3.DatabaseError as exc:
            raise ValueError("搜索索引无法读取，请从搜索页重建索引。") from exc
        if not row:
            return None
        # Never present a cached old note or a changed PDF as current source text.
        sources, _ = self.sources()
        source = next((source for source in sources if source.key == row["source"]), None)
        if source is None or source.signature != row["signature"]:
            raise ValueError("来源已更新或移除，请先更新搜索索引后重新搜索。")
        return dict(row)


def snippet(text: str, terms: list[str]) -> str:
    # Match with original offsets where possible; HTML is always escaped by Jinja.
    match = re.search("|".join(re.escape(term) for term in terms), text, re.IGNORECASE) if terms else None
    offset = match.start() if match else 0
    if not match and terms:
        normalized = normalize(text)
        positions = [normalized.find(term) for term in terms if term in normalized]
        if positions:
            # NFKC and case folding may change lengths (e.g. full-width words,
            # ligatures). Map the normalized hit back to a nearby original offset.
            target = min(positions)
            low, high = 0, len(text)
            while low < high:
                middle = (low + high) // 2
                if len(normalize(text[:middle])) < target:
                    low = middle + 1
                else:
                    high = middle
            offset = low
    start = max(0, offset - 80)
    return ("…" if start else "") + text[start:start + 300] + ("…" if len(text) > start + 300 else "")
