"""Presentation for task receipts, separate from long-form paper reading."""
from __future__ import annotations

import html
import re
from pathlib import Path

from bs4 import BeautifulSoup

from .render import _local_asset_base, _reader_asset_url, markdown_with_math
from .utils import atomic_write_text

KINDS = {
    'tracking': ('版本检查结果', 'VERSION WATCH', '本次检查的记录；切换来源只筛选此页，不会发起同步。'),
    'batch': ('批量同步结果', 'BATCH SYNC', '保留本次选择的目标版本与执行步骤，可返回追踪页继续未完成任务。'),
    'sync': ('论文同步记录', 'PAPER SYNC', '按版本保留执行记录、PDF 与报告，重试沿用原目标版本。'),
    'diff': ('版本差异', 'VERSION DIFF', '核对方法、实验与结论的变化，阅读前确认差异分析的证据范围。'),
}


def artifact_kind(path: str) -> str | None:
    parts = Path(path).parts
    if not parts:
        return None
    name = parts[-1]
    if parts[0] == 'versions':
        if len(parts) == 2 and name.startswith('index-') and name.endswith('.html'):
            return 'tracking'
        if name == 'report.html':
            return 'diff'
    if parts[0] == 'version-batches' and len(parts) == 4 and name == 'index.html':
        return 'batch'
    if parts[0] == 'papers' and len(parts) == 4 and name == 'index.html':
        return 'sync'
    return None


def _tracking_cards(tree):
    """Read the saved list, never substitute today's mutable tracking state."""
    heading = next((h for h in tree.find_all('h2') if h.get_text(strip=True) == '正在追踪'), None)
    if heading is None:
        return [], ''
    listing = heading.find_next_sibling('ul')
    if listing is None:
        return [], ''
    cards, records = [], []
    for item in listing.find_all('li', recursive=False):
        link = item.find('a')
        text = item.get_text(' ', strip=True)
        aid = re.search(r'arXiv:([^\s·]+)', text)
        if link is None or aid is None:
            continue
        title = link.get_text().strip()
        # arXiv titles sometimes contain bare font commands without math delimiters.
        title = re.sub(r'\\(?:boldsymbol|mathbf|mathrm|textbf)\{([^{}]*)\}', r'\1', title)
        tail = text.split('arXiv:', 1)[1]
        match = re.fullmatch(r'\s*v([1-9]\d*)\s*', tail.split('·')[1]) if '·' in tail else None
        latest = f'最新 v{match[1]}' if match else '版本待核实'
        sources = [label for label in ('本地报告', 'Zotero') if label in tail]
        source_names = ' '.join('reports' if label == '本地报告' else 'zotero' for label in sources)
        records.append({'sources': sources, 'unknown': not match})
        badges = ''.join(f'<span class="source-chip">{label}</span>' for label in sources)
        # New snapshots include independent material versions after the source label.
        details = tail.split('·', 2)[-1].strip() if tail.count('·') >= 2 else ''
        if '（' not in details:
            details = ''
        url = str(link.get('href') or ('https://arxiv.org/abs/' + aid[1]))
        cards.append(f'<article class="result-paper" data-sources="{source_names}" data-search="{html.escape(title + " " + aid[1], quote=True)}">'
                     f'<div><h3><a href="{html.escape(url, quote=True)}">{html.escape(title)}</a></h3>'
                     f'<p class="paper-id">arXiv:{html.escape(aid[1])}</p><div class="source-chips">{badges}</div>'
                     f'<p class="material-detail">{html.escape(details)}</p></div>'
                     f'<span class="version-chip {"unknown" if not match else ""}">{latest}</span></article>')
    if len(records) != len(listing.find_all('li', recursive=False)):
        return [], ''  # Preserve unfamiliar legacy content instead of silently dropping it.
    listing.decompose()
    heading.decompose()
    return records, ''.join(cards)


def task_document(source: str, title: str, kind: str, *, asset_base: str = '/static') -> str:
    label, kicker, description = KINDS[kind]
    body, _ = markdown_with_math(source)
    tree = BeautifulSoup(body, 'html.parser')
    first = tree.find('h1')
    if first:
        title = first.get_text(' ', strip=True)
        first.decompose()
    # Legacy missing values are presentation defects; keep the original file intact.
    if kind == 'tracking':
        for node in tree.find_all(string=True):
            node.replace_with(re.sub(r'\bv(?:None|null|0)\b', '版本待核实', str(node)))
    records, cards = _tracking_cards(tree) if kind == 'tracking' else ([], '')
    sections: list[tuple[str, str]] = []
    current: list[str] = []
    heading = ''
    for node in list(tree.contents):
        if getattr(node, 'name', None) == 'h2':
            if heading or ''.join(current).strip():
                sections.append((heading, ''.join(current)))
            heading, current = node.get_text(' ', strip=True), []
        else:
            current.append(str(node))
    if heading or ''.join(current).strip():
        sections.append((heading, ''.join(current)))
    intro = ''
    if sections and not sections[0][0]:
        intro = sections.pop(0)[1]
    panels = ''.join(f'<section class="result-panel"><h2>{html.escape(name)}</h2><div class="result-prose">{content}</div></section>' for name, content in sections)
    stats, filters, paper_list = '', '', ''
    if cards:
        counts = [('记录论文', len(records)), ('含本地报告', sum('本地报告' in r['sources'] for r in records)),
                  ('含 Zotero', sum('Zotero' in r['sources'] for r in records)), ('最新版本待核实', sum(r['unknown'] for r in records))]
        stats = '<dl class="result-stats">' + ''.join(f'<div><dt>{name}</dt><dd>{count}<small>篇</small></dd></div>' for name, count in counts) + '</dl>'
        filters = '<div class="result-filters"><div role="group" aria-label="按来源查看">' + ''.join(
            f'<button type="button" data-scope="{key}" aria-pressed="{str(key == "all").lower()}">{name}</button>'
            for key, name in [('all', '全部'), ('reports', '本地报告'), ('zotero', 'Zotero')]) + '</div><label><span class="sr-only">查找论文</span><input type="search" placeholder="搜索标题或 arXiv ID" aria-label="查找论文"></label></div>'
        paper_list = f'<section class="result-library"><div class="list-heading"><h2>追踪记录</h2><span id="result-count" role="status">{len(records)} 篇</span></div>{filters}<div class="result-papers">{cards}</div><p id="result-empty" hidden>没有符合条件的论文，试试其他来源或关键词。</p></section>'
    assets = html.escape(asset_base.rstrip('/'), quote=True)
    return f'''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>{html.escape(title)} · PaperLoom</title>
<link rel="icon" href="{assets}/app-icon.ico" sizes="any"><link rel="stylesheet" href="{_reader_asset_url(asset_base, 'task-result.css')}"><link rel="stylesheet" href="{assets}/vendor/katex/katex.min.css"></head>
<body class="task-result task-{kind}"><header class="result-topbar"><a class="result-brand" href="/versions"><img src="{assets}/app-icon.ico" alt="" width="30" height="30">PaperLoom<span>研究工作台</span></a><a class="back-link" href="/versions">← 返回版本追踪</a></header>
<main class="result-main"><nav class="result-breadcrumb" aria-label="当前位置"><a href="/versions">版本追踪与同步</a><span>/</span>{label}</nav>
<header class="result-heading"><div><p class="result-kicker">{kicker}</p><h1>{label}</h1><p class="result-subtitle">{html.escape(title)}</p></div><button type="button" class="print-result">打印 / 保存 PDF</button></header>
<p class="result-description">{description}</p><div class="result-intro result-prose">{intro}</div>{stats}{panels}{paper_list}
<footer class="result-footer">PaperLoom · 本页保留任务生成时的记录。<a href="/versions">查看当前追踪状态 →</a></footer></main>
<script defer src="{assets}/vendor/katex/katex.min.js"></script><script defer src="{_reader_asset_url(asset_base, 'task-result.js')}"></script></body></html>'''


def render_task_result(source: str, destination: Path, title: str, kind: str) -> None:
    atomic_write_text(destination.with_suffix('.md'), source)
    atomic_write_text(destination, task_document(source, title, kind, asset_base=_local_asset_base(destination)))
