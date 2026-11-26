"""Decide which concrete overview evidence gaps still warrant a lookup or a reading."""
from __future__ import annotations

from dataclasses import dataclass

from ..papers.paper_data import base_id, requested_version
from ..runtime.model_access.settings import ARXIV_ID_RE


@dataclass(frozen=True)
class OverviewFollowup:
    question: str
    available: str
    tool: str
    query: str
    alternate_query: str
    requires_original: bool

    @classmethod
    def from_selection(cls, items):
        result = []
        for item in items:
            if not isinstance(item, dict):
                continue
            fields = {}
            for key in ("question", "available", "tool", "query", "alternate_query"):
                value = item.get(key)
                fields[key] = value.strip()[:250] if isinstance(value, str) else ""
            if not fields["question"] or not fields["query"] or fields["tool"] not in ("title", "topic", "id"):
                continue
            if fields["tool"] == "id" and not ARXIV_ID_RE.fullmatch(fields["query"]):
                continue
            result.append(cls(**fields, requires_original=item.get("requires_original") is True))
        return result

    def lookups(self):
        yield self.tool, self.query
        alternate = self.alternate_query
        if self.tool == "title" and not alternate:
            alternate = self.query.partition(":")[0].strip()
        if alternate and alternate.casefold() != self.query.casefold():
            yield ("title" if self.tool == "title" else "topic"), alternate
        elif self.tool == "title":
            yield "topic", self.query

    def assess(self, search, sources):
        """Return the next distinct action and an evidence-based note for the author."""
        lookups = list(self.lookups())
        keys = {(tool, query.casefold()) for tool, query in lookups}
        outcomes = [o for o in search.outcomes if (o.tool, o.query.casefold()) in keys]
        found = {aid for outcome in outcomes for aid, _ in outcome.found}
        prefix = f"{self.question}（{self.query}）"
        if self.tool == "id":
            version = requested_version(self.query)
            matched = [s for s in sources if s["arxiv_id"] == base_id(self.query)
                       and (version is None or s["version"] == version)]
        elif self.tool == "title":
            # A title search can return related papers. For a named target, use
            # the title (or its distinctive prefix) to identify the selected hit.
            title = self.query.casefold()
            short = title.partition(":")[0].strip()
            matched = [s for s in sources if s["title"].casefold() == title or (
                s["arxiv_id"] in found and s["title"].casefold().partition(":")[0].strip() == short)]
        else:
            matched = [s for s in sources if s["arxiv_id"] in found]
        if matched:
            if not self.requires_original or any(s["material"] == "fulltext" for s in matched):
                return None, ""
            read_ids = {(o.query, o.read_version) for o in search.outcomes if o.tool == "read"}
            source = next((s for s in matched if (s["arxiv_id"], s["version"]) not in read_ids), matched[0])
            aid = source["arxiv_id"]
            label = "仅取得摘要" if source["material"] == "abstract" else "仅取得本地报告摘录"
            note = f"{prefix}：{source['title']} {label}，尚无可供比较的原文摘录。"
            if (aid, source["version"]) in read_ids:
                return None, note
            return self._action("read", aid), note

        tried = {(row.tool, row.query.casefold()) for row in outcomes}
        action = next((self._action(tool, query) for tool, query in lookups
                       if (tool, query.casefold()) not in tried), None)
        explanations = []
        for row in outcomes:
            if row.error:
                outcome = "请求未完成（" + row.error + "）"
            elif row.found:
                outcome = "命中候选，入选材料中未确认所需文献"
            else:
                outcome = "未命中"
            explanations.append(f"{row.query} {outcome}")
        status = "；".join(explanations) or "本次未执行该项定向检索"
        if action and not search.remaining:
            status += "，剩余额度不足以继续补查"
        return action, f"{prefix}：{status}。"

    def _action(self, tool, query):
        reason = self.question
        if self.available:
            reason += "（已有材料：" + self.available + "）"
        return {"tool": tool, "query": query, "reason": reason}
