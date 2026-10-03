"""Move legacy table appendices into report discussions, with reversible backups."""
from __future__ import annotations

import argparse
import hashlib
from pathlib import Path
import re
import shutil

import fitz

from arxiv_ra.evidence import TOKEN, attach_evidence, source_table_inventory
from arxiv_ra.models import ParsedPaper
from arxiv_ra.render import _local_asset_base, report_document
from arxiv_ra.report_completeness import _headings, normalize_table_titles, restore_note_tables
from arxiv_ra.utils import atomic_write_text, read_json, write_json
from arxiv_ra.quality import normalized_excerpt
from arxiv_ra.source_spans import exact_span, source_spans


def integrate(directory: Path, root: Path, *, apply=False, parsed_file: Path | None = None, titles_only=False, audit_only=False):
    path = directory / 'report.md'
    original = path.read_text(encoding='utf-8')
    old_evidence = read_json(directory / 'evidence.json')
    if audit_only:
        if old_evidence.get('numeric_audit', {}).get('publication_policy') == 'report_diagnostics_v3':
            return False
    elif titles_only:
        if normalize_table_titles(original) == original:
            return False
    elif not any(h[3] == '原文表格摘录' for h in _headings(original)):
        return False
    metadata = read_json(directory / 'metadata.json')
    if parsed_file:
        raw = read_json(parsed_file)['parsed']
        parsed = ParsedPaper(raw['text'], raw['page_texts'], total_pages=raw['total_pages'])
    else:
        with fitz.open(directory / 'paper.pdf') as pdf:
            pages = [page.get_text('text') for page in pdf]
        parsed = ParsedPaper('\n\n'.join(pages), pages, total_pages=len(pages))
    inventory = source_table_inventory(parsed)
    # Remove generated audit appendices before regenerating them for the new
    # text offsets, sections and citation numbering. Keep content uncertainty.
    report = original
    # Undo uniquely identifiable old cell suppression before rechecking. Keep
    # the previous audit too, including originals whose locations are ambiguous.
    citations_list = old_evidence.get('citations', [])
    def rendered(match):
        citation = next((c for c in citations_list if (
            c.get('source_id') == match[2].strip() if match[2] is not None else
            normalized_excerpt(c['quote']) == normalized_excerpt(match[1]))), None)
        return f'[{citation["id"]}](paper.pdf#page={citation["page"]})' if citation else '—'
    unresolved = []
    for issue in old_evidence.get('numeric_audit', {}).get('issues', []):
        if issue.get('action') not in {'withhold_cells', 'withhold_claim', 'withhold_clauses'}:
            continue
        before, after = (TOKEN.sub(rendered, issue[key]) for key in ('replacement', 'original'))
        if report.count(before) == 1:
            report = report.replace(before, after, 1)
        else:
            # Keep ambiguously located originals available to the reader;
            # never guess which identical placeholder they belong to.
            if issue.get('action') == 'withhold_cells':
                check = next((c for c in old_evidence.get('numeric_audit', {}).get('table_checks', [])
                              if c.get('start') == issue.get('start')), None)
                if check:
                    headers = check['headers']
                    after = '| ' + ' | '.join(headers) + ' |\n| ' + ' | '.join(['---'] * len(headers)) + ' |\n' + after
                else:
                    after = '```text\n' + after + '\n```'
            unresolved.append(after)
    if unresolved:
        report += '\n\n## 恢复的报告内容\n\n以下内容来自之前保存的报告，原位置无法唯一确定。\n\n' + '\n\n'.join(unresolved) + '\n'
    headings = _headings(report)
    for heading in reversed(headings):
        if heading[3] in {'引用与核对', '原文依据与覆盖', '原文表格索引'}:
            end = next((h[0] for h in headings if h[0] > heading[0] and h[2] <= heading[2]), len(report))
            report = report[:heading[0]] + report[end:]
    report = re.sub(r'(?m)^> \*\*实验数值待核对\*\*[^\n]*\n?', '', report)
    report = report.replace('**[待核对]** ', '')
    citations = {str(c['id']): c for c in old_evidence.get('citations', [])}
    spans = source_spans(parsed)

    def source(match):
        citation = citations.get(match[1])
        if not citation or str(citation['page']) != match[2]:
            raise ValueError('Stored citation link has no matching evidence record')
        # Revalidate exact coordinates before preserving a stored ID. A raw
        # quote alone cannot distinguish repeated text on different pages.
        key = citation.get('source_id')
        span = (exact_span(key, parsed) or spans.get(key)) if key else None
        if span and span['page'] == citation['page'] and normalized_excerpt(span['quote']) == normalized_excerpt(citation['quote']):
            return f'[[证据ID:{key}]]'
        return '[[证据:' + citation['quote'] + ']]'

    report = re.sub(r'\[(\d+)\]\(paper\.pdf#page=(\d+)(?: "[^"]*")?\)', source, report)
    report = restore_note_tables(report, [], inventory)
    report, evidence = attach_evidence(report, parsed, pdf_available=True,
                                      full_report=metadata.get('report_quality') == 'full')
    evidence['previous_numeric_audit'] = old_evidence.get('numeric_audit', {})
    if not apply:
        print(directory.name, 'preview: tables integrated, audit rebuilt')
        return report, evidence
    digest = hashlib.sha256(original.encode()).hexdigest()[:12]
    backup = root / '.jobs' / 'table-integration-backups' / directory.relative_to(root) / digest
    backup.mkdir(parents=True, exist_ok=True)
    for name in ('report.md', 'report.html', 'metadata.json', 'evidence.json'):
        if (directory / name).is_file() and not (backup / name).exists():
            shutil.copy2(directory / name, backup / name)
    metadata['evidence'] = evidence
    atomic_write_text(backup / 'integrated.html', report_document(
        report, metadata['paper']['title'], arxiv_id=metadata['paper']['arxiv_id'],
        profile_id=metadata['profile_id'], report_id=(directory / 'report.html').relative_to(root).as_posix(),
        asset_base=_local_asset_base(directory / 'report.html')))
    atomic_write_text(path, report)
    write_json(directory / 'evidence.json', evidence)
    write_json(directory / 'metadata.json', metadata)
    (backup / 'integrated.html').replace(directory / 'report.html')
    print(directory.name, 'applied: tables integrated, audit rebuilt')
    return True


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('root', type=Path)
    parser.add_argument('--paper', default='')
    parser.add_argument('--parsed', type=Path, help='Stored parsed.json for a single selected report')
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--titles-only', action='store_true', help='Remove legacy extraction labels and rebuild audit details')
    parser.add_argument('--audit-only', action='store_true', help='Upgrade numeric publication policy without hiding original content')
    args = parser.parse_args()
    root = args.root.resolve()
    directories = [p.parent for p in root.glob('????-??-??/reports/*/report.md')
                   if not args.paper or args.paper in p.parent.name]
    if args.parsed and len(directories) != 1:
        parser.error('--parsed requires exactly one selected report')
    for directory in sorted(directories):
        integrate(directory, root, apply=args.apply, parsed_file=args.parsed, titles_only=args.titles_only, audit_only=args.audit_only)
