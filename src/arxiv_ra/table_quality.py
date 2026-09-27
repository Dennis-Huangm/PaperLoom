"""Conservative row/column checks. Never reconstruct a damaged PDF table.

Explicit pipe tables permit exact header bindings. Plain PDF text permits a
weaker row/order check only; passing that check is NOT metric verification.
"""
from __future__ import annotations

import re
import unicodedata

from .markdown_rendering import _CODE_SPAN, _TABLE_MATH
from .quality import numbers


def mask_quotes(text, token):
    # Keep character offsets, but hide source newlines, pipes and headings.
    return token.sub(lambda m: "X" * len(m[0]), text)


def cells(line):
    """Return cell text and offsets without splitting escaped/code/math pipes."""
    line = line.rstrip("\r\n")
    protected = _CODE_SPAN.sub(lambda m: m[0].replace("|", "X"), line)
    protected = _TABLE_MATH.sub(lambda m: m[0].replace("|", "X"), protected)
    cuts = []
    for match in re.finditer(r"\|", protected):
        before = protected[:match.start()]
        if (len(before) - len(before.rstrip("\\"))) % 2 == 0:
            cuts.append(match.start())
    if not cuts:
        return []
    bounds = [-1, *cuts, len(line)]
    result = [(line[a + 1:b].strip(), a + 1, b) for a, b in zip(bounds, bounds[1:])]
    if not result[0][0]:
        result.pop(0)
    if result and not result[-1][0]:
        result.pop()
    return result


def tables(text, token):
    masked = mask_quotes(text, token)
    lines = masked.splitlines(keepends=True)
    offsets, offset = [], 0
    for line in lines:
        offsets.append(offset)
        offset += len(line)
    fence = None
    i = 0
    while i + 1 < len(lines):
        marker = re.match(r"^ {0,3}(`{3,}|~{3,})", lines[i])
        if marker:
            if fence is None:
                fence = marker[1]
            elif marker[1][0] == fence[0] and len(marker[1]) >= len(fence):
                fence = None
        header = cells(lines[i]) if not fence else []
        separator = cells(lines[i + 1])
        if not header or len(separator) != len(header) or not all(re.fullmatch(r":?-+:?", c[0]) for c in separator):
            i += 1
            continue
        headers = [c[0] for c in header]
        header_start = offsets[i]
        i += 2
        while i < len(lines) and (row := cells(lines[i])) and len(row) == len(headers):
            start = offsets[i]
            yield {"headers": headers, "cells": row, "start": start,
                   "header_start": header_start,
                   "end": start + len(lines[i].rstrip("\r\n")),
                   "raw": text[start:start + len(lines[i].rstrip("\r\n"))]}
            i += 1


def label(text):
    value = unicodedata.normalize("NFKC", text).strip().casefold()
    value = re.sub(r"\[\d+(?:,\s*\d+)*\][†‡⋆★☆*]*\s*$", "", value)
    return re.sub(r"\s+", " ", value.strip(" *`†‡⋆★☆"))


def scalar(text):
    value = text.strip(" *`")
    found = numbers(value)
    return next(iter(found)) if len(found) == 1 and re.fullmatch(
        r"[+−-]?(?:\d[\d,]*(?:\.\d+)?)(?:[eE][+-]?\d+)?\s*(?:%|ms|s|GB|MB|GiB|MiB|k)?", value) else None


def _plain_rows(page, owner):
    """Only accept exact row names and separate scalar tokens; no digit repair."""
    lines = page.splitlines(keepends=True)
    offset = 0
    for index, line in enumerate(lines):
        clean = label(line)
        # PDF rows can be one cell per line or one whole row per line.
        match = re.match(re.escape(owner) + r"(?:\s+\[\d+(?:,\s*\d+)*\][†‡⋆★☆*]*)?(?=\s|$)(.*)$", clean)
        if match:
            values = []
            tail = match[1].strip()
            if tail:
                pieces = tail.split()
                if all(scalar(x) is not None for x in pieces):
                    values.extend(scalar(x) for x in pieces)
                else:
                    offset += len(line)
                    continue
            end = offset + len(line)
            damaged = False
            for following in lines[index + 1:index + 65]:
                pieces = following.split()
                if not pieces or not all(scalar(x) is not None for x in pieces):
                    damaged = bool(pieces and re.match(r"^[+−-]?\d", pieces[0]))
                    break
                values.extend(scalar(x) for x in pieces)
                end += len(following)
            if values and not damaged:
                # A caption is a hard context boundary; don't borrow difficulty
                # labels from a different table on the same page.
                preceding = "".join(lines[:index])
                boundary = list(re.finditer(r"(?im)^\s*(?:table|表)\s*\d+\s*[:：]", preceding))
                context = preceding[boundary[-1].start():] if boundary else preceding
                yield {"start": offset, "end": end, "values": values, "context": context}
        offset += len(line)


_OWNER = {"method", "model", "模型", "方法", "模型/方法", "benchmark", "基准", "metric", "指标", "level"}
_CONTEXT = {"difficulty", "难度", "task", "任务", "dataset", "数据集", "split", "划分", "setting", "设置"}


def _header(text):
    value = label(text)
    # Models routinely use valid inline LaTeX for the same direction glyph.
    # Normalize only an entire scalar arrow span, not arbitrary math/units.
    value = re.sub(r'\\\(\s*\\(uparrow|downarrow)\s*\\\)|\$\s*\\(uparrow|downarrow)\s*\$',
                   lambda m: '↑' if (m[1] or m[2]) == 'uparrow' else '↓', value)
    split = re.search(r"[（(↑↓]", value)
    if not split:
        return value, ""
    qualifier = value[split.start():]
    direction = "".join(sorted(set(re.findall(r"[↑↓]", qualifier))))
    qualifier = re.sub(r"[()\s↑↓]", "", qualifier) + direction
    return value[:split.start()].strip(), qualifier


def _column_scalar(text, header):
    value = scalar(text)
    if value is None:
        return None
    unit = re.search(r"\(\s*(%|ms|s|GB|MB|GiB|MiB)\s*\)", header)
    if not unit:
        return value
    if value[1] and value[1] != unit[1]:
        return None
    return value[0], unit[1]


def _plain_header_check(candidate, headers, values, numeric):
    """Recognize ONLY a flat, unique header with one cell per PDF text line.

    Repeated ACC, grouped task headers and wrapped names must abstain. The
    actual source row width fixes the required header width, not the report.
    """
    lines = [x.strip() for x in candidate["context"].splitlines()]
    starts = [i for i, line in enumerate(lines) if label(line) in _OWNER]
    if not starts:
        return [], []
    width = len(candidate["values"])
    selected = lines[starts[-1] + 1:starts[-1] + 1 + width]
    if len(selected) != width or any(scalar(x) is not None for x in selected):
        return [], []
    mapping = {_header(h)[0]: i for i, h in enumerate(selected)}
    if len(mapping) != width:
        return [], []
    # The first data row must immediately follow these headers. Otherwise a
    # grouped/extra header could shift every column by one.
    following = lines[starts[-1] + 1 + width:]
    if following and (len(following) < width + 1 or not all(scalar(x) is not None for x in following[1:width + 1])):
        return [], []
    supported, bad = [], []
    for col in numeric:
        name, qualifier = _header(headers[col])
        if name not in mapping:
            continue
        index = mapping[name]
        source_value = candidate["values"][index]
        source_unit = re.search(r"\(\s*(%|ms|s|GB|MB|GiB|MiB)\s*\)", selected[index])
        if source_unit and not source_value[1]:
            source_value = source_value[0], source_unit[1]
        if qualifier == _header(selected[index])[1] and _column_scalar(values[col], headers[col]) == source_value:
            supported.append(col)
        else:
            bad.append(col)
    return supported, bad


def check_row(row, sources, pages):
    """Return a bounded check with offending column indices, or abstain.

    Only the pages actually cited by the row may be consulted. A candidate row
    must intersect the cited excerpt; unrelated pages cannot rescue a claim.
    """
    headers, values = row["headers"], [c[0] for c in row["cells"]]
    owner_columns = [i for i, h in enumerate(headers) if _header(h)[0] in _OWNER]
    numeric = {i: scalar(v) for i, v in enumerate(values) if scalar(v) is not None}
    if len(owner_columns) != 1 or not numeric:
        return {"status": "unassessed", "reason": "unsupported_table_shape", "bad_columns": []}
    owner_col = owner_columns[0]
    owner = label(values[owner_col])
    contexts = [label(values[i]) for i, h in enumerate(headers) if label(h) in _CONTEXT]
    candidates, explicit, conflicts = [], [], []
    for source in sources:
        page = pages[source["page"] - 1]
        quote = source["quote"]
        # Strongest case: a real Markdown/pipe source table with exact headers.
        for source_row in tables(page, re.compile(r"(?!x)x")):
            sh = source_row["headers"]
            sv = [c[0] for c in source_row["cells"]]
            mapping = {_header(h)[0]: j for j, h in enumerate(sh)}
            if len(mapping) != len(sh) or any(_header(h)[0] not in mapping for h in headers if label(h) not in {"依据", "原文依据"}):
                continue
            if label(sv[mapping[_header(headers[owner_col])[0]]]) != owner:
                continue
            if not _overlaps(source_row, source, quote, page):
                continue
            if any(label(sv[mapping[_header(h)[0]]]) != label(values[i]) for i, h in enumerate(headers) if label(h) in _CONTEXT):
                conflicts.append([source["page"], source_row["start"], source_row["end"]])
                continue
            bad = [i for i in numeric if _column_scalar(sv[mapping[_header(headers[i])[0]]], sh[mapping[_header(headers[i])[0]]])
                   != _column_scalar(values[i], headers[i])
                   or _column_scalar(values[i], headers[i]) is None
                   or _header(headers[i])[1] != _header(sh[mapping[_header(headers[i])[0]]])[1]]
            explicit.append((bad, source["page"], source_row["start"], source_row["end"]))
        for candidate in _plain_rows(page, owner):
            if not _overlaps(candidate, source, quote, page):
                continue
            if contexts:
                # Context names must be explicit standalone source labels.
                context_lines = [label(x) for x in candidate["context"].splitlines()]
                # Difficulty labels are a closed presentation vocabulary, not
                # model names or an inferred experimental condition.
                levels = [x for x in context_lines if x in {"easy", "medium", "med.", "hard"}]
                if any(c in {"easy", "medium", "med.", "hard"} and (not levels or levels[-1] != c) for c in contexts):
                    if levels:
                        conflicts.append([source["page"], candidate["start"], candidate["end"]])
                    continue
                if any(c not in context_lines for c in contexts):
                    continue
            supported, bad = _plain_header_check(candidate, headers, values, numeric)
            candidates.append((candidate["values"], source["page"], candidate["start"], candidate["end"], supported, bad))
    if explicit:
        distinct = {tuple(item[0]) for item in explicit}
        if len(distinct) == 1:
            bad = explicit[0][0]
            return {"status": "mismatch" if bad else "headers_checked", "reason": "column_value_mismatch" if bad else "",
                    "bad_columns": bad, "supported_columns": [i for i in numeric if i not in bad],
                    "source_rows": [list(x[1:]) for x in explicit]}
        return {"status": "unassessed", "reason": "conflicting_source_rows", "bad_columns": []}
    candidates = list({(tuple(x[0]), *x[1:4]): x for x in candidates}.values())
    if not candidates and conflicts:
        return {"status": "mismatch", "reason": "row_context_mismatch",
                "bad_columns": list(numeric), "source_rows": conflicts}
    if not candidates or len({tuple(x[0]) for x in candidates}) > 1:
        return {"status": "unassessed", "reason": "row_not_uniquely_resolved", "bad_columns": []}
    sequence = candidates[0][0]
    supported = set.intersection(*(set(c[4]) for c in candidates))
    header_bad = set.union(*(set(c[5]) for c in candidates))
    if supported or header_bad:
        # Exact flat-header bindings allow a deliberate column reordering;
        # fall back to the weaker ordered row check for other columns.
        unbound = [i for i in numeric if i not in supported and i not in header_bad]
        bad = sorted(header_bad | {i for i in unbound if numeric[i] not in sequence})
        return {"status": "mismatch" if bad else "headers_checked" if not unbound else "partial_headers_checked",
                "reason": "column_value_mismatch" if bad else "headers_not_assessed" if unbound else "",
                "bad_columns": bad, "supported_columns": sorted(supported - header_bad),
                "source_rows": [list(x[1:4]) for x in candidates]}
    selected = list(numeric.values())
    # A subset is allowed (e.g. intentionally omitting RLD). It must preserve
    # source order and multiplicity, so a swapped column cannot pass set lookup.
    position = 0
    ordered = True
    for value in selected:
        try:
            position = sequence.index(value, position) + 1
        except ValueError:
            ordered = False
            break
    bad = [i for i, value in numeric.items() if value not in sequence]
    if not ordered and not bad:
        bad = list(numeric)  # Cannot attribute the swap safely to just one cell.
    return {"status": "mismatch" if bad else "row_order_checked",
            "reason": "row_value_or_order_mismatch" if bad else "headers_not_assessed",
            "bad_columns": bad, "source_rows": [list(x[1:4]) for x in candidates]}


def _overlaps(row, source, quote, page):
    if "start" in source:
        return max(row["start"], source["start"]) < min(row["end"], source["end"])
    # Legacy quotations may flatten whitespace; accept only a complete row
    # inside the located quote, never infer a raw offset from normalized text.
    return re.sub(r"\s+", "", page[row["start"]:row["end"]]) in re.sub(r"\s+", "", quote)
