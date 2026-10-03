"""Protect structured report front matter from model re-spelling."""
from __future__ import annotations

import re

from .activity import literal
from .models import Paper, VerifiedMetadata
from .table_quality import cells


_FIELDS = {'中文标题', '标题', '作者', '作者及机构', '作者来源', 'arXiv ID', 'arXiv 修订版',
           'arXiv类别', 'arXiv 类别', '首次公开日期', '修订日期', '正式发表日期',
           '会议或期刊', '会议/期刊', '状态', '会议状态', 'DOI', '链接', '元数据来源'}


def _field(label):
    label = label.strip(' *`')
    if re.fullmatch(r'(?:机构|作者机构|所属机构)(?:[（(][^\n）)]*[）)])?', label):
        return '机构'
    return label if label in _FIELDS else None


def _front_fields(front):
    """Consume structured fields (table/list) while preserving unrelated prose."""
    values, extras = {}, []
    active = None
    for line in front.splitlines():
        row = cells(line) if line.lstrip().startswith('|') else []
        if len(row) >= 2:
            key = _field(row[0][0])
            if key:
                # Structured affiliations have their own authoritative field;
                # do not replace the separate extracted institution text.
                if row[0][0].strip(' *') != '机构（结构化元数据）':
                    values.setdefault(key, ' | '.join(cell[0] for cell in row[1:]))
            active = None
            continue
        match = re.match(r'^\s*(?:[-+*]|\d+[.)])\s+(.+?)\s*[：:]\s*(.*)$', line)
        if match and (key := _field(match[1])):
            active = key
            values.setdefault(key, match[2].lstrip('* ').strip())
            continue
        if active and line.startswith(('  ', '\t')) and line.strip():
            values[active] += ' ' + line.strip()
            continue
        active = None
        if not line.lstrip().startswith('#'):
            extras.append(line)
    return values, '\n'.join(extras).strip()


def protect_metadata(report: str, paper: Paper, metadata: VerifiedMetadata) -> str:
    """Replace front-matter tables only; leave claims in the body for review.

    Author names prefer the paper snapshot (preserving arXiv diacritics).
    No positional/fuzzy author-to-institution join is inferred.
    """
    match = re.search(r"(?m)^##\s+(?!基本信息(?:表)?\s*$)[^\n]+", report)
    end = match.start() if match else len(report)
    front, body = report[:end], report[end:]
    values, extras = _front_fields(front)
    translated, institutions = values.get('中文标题', ''), values.get('机构', '')
    authors = paper.authors or metadata.authors
    affiliations = list(dict.fromkeys(a for author in [*paper.authors, *metadata.authors]
                                      for a in author.affiliations if a.strip()))
    fields = [
        ("作者", "、".join(a.name for a in authors) or "未核实"),
        ("作者来源", paper.metadata_label if paper.authors else "结构化核验元数据" if metadata.authors else "未核实"),
        ("机构（结构化元数据）", "；".join(affiliations) or "未核实"),
        ("arXiv ID", paper.arxiv_id),
        ("arXiv 修订版", str(paper.version) if paper.version is not None else "未核实"),
        ("arXiv 类别", ", ".join(paper.categories) or paper.primary_category or "未核实"),
        ("首次公开日期", paper.published.date().isoformat() if paper.published else "未核实"),
        ("修订日期", paper.updated.date().isoformat() if paper.updated else "未核实"),
        ("正式发表日期", metadata.publication_date or "未核实"),
        ("会议或期刊", metadata.venue or "未核实"),
        ("状态", metadata.venue_status),
        ("DOI", metadata.doi or "未核实"),
        ("链接", paper.abs_url or "未核实"),
        ("元数据来源", ", ".join(metadata.sources) or "未核实"),
    ]
    table = ["| 字段 | 内容 |", "| --- | --- |"]
    # Preserve existing Markdown/citations while making literal field pipes
    # safe. Entity encoding is idempotent across the two publication passes.
    safe_cell = lambda value: re.sub(r'(?<!\\)\|', '&#124;', value)
    table.append(f"| 中文标题 | {safe_cell(translated) or '未提供'} |")
    table.extend(f"| {label} | {literal(' '.join(str(value).split()))} |" for label, value in fields)
    table.append(f"| 机构（论文摘录，未核实） | {safe_cell(institutions) or '未核实'} |")
    # Retain front-matter notices, but replace all model front-matter tables,
    # headings and H1 titles so duplicate author rows cannot survive.
    title = literal(' '.join((paper.title or metadata.title).split()))
    return f"# {title}\n\n" + "\n".join(table) + (
        "\n\n" + extras if extras else "") + "\n\n" + body.lstrip()
