"""Protect structured report front matter from model re-spelling."""
from __future__ import annotations

import re

from .activity import literal
from .models import Paper, VerifiedMetadata


def protect_metadata(report: str, paper: Paper, metadata: VerifiedMetadata) -> str:
    """Replace front-matter tables only; leave claims in the body for review.

    Author names prefer the paper snapshot (preserving arXiv diacritics).
    No positional/fuzzy author-to-institution join is inferred.
    """
    match = re.search(r"(?m)^##\s+(?!基本信息(?:表)?\s*$)[^\n]+", report)
    end = match.start() if match else len(report)
    front, body = report[:end], report[end:]
    translated, institutions = "", ""
    for line in front.splitlines():
        cells = re.split(r"(?<!\\)\|", line.strip())
        if len(cells) < 4:
            continue
        label, value = cells[1].strip().strip("* "), cells[2].strip()
        if label == "中文标题":
            translated = value
        if label in {"机构", "作者机构", "所属机构", "机构（论文原文）", "机构（论文摘录，未核实）"}:
            # Preserve only a separate institution field, never a combined author
            # field containing re-spelled names or an invented author mapping.
            institutions = value
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
    if translated:
        table.append(f"| 中文标题 | {translated} |")
    table.extend(f"| {label} | {literal(' '.join(str(value).split()))} |" for label, value in fields)
    if institutions and institutions != "未核实":
        table.append(f"| 机构（论文摘录，未核实） | {institutions} |")
    # Retain front-matter notices, but replace all model front-matter tables,
    # headings and H1 titles so duplicate author rows cannot survive.
    extras = "\n".join(line for line in front.splitlines()
                       if not line.lstrip().startswith(("|", "#"))).strip()
    title = literal(' '.join((paper.title or metadata.title).split()))
    return f"# {title}\n\n" + "\n".join(table) + (
        "\n\n" + extras if extras else "") + "\n\n" + body.lstrip()
