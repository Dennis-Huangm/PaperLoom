"""Reapply table publication policy to a saved report without model calls.

Use the original parsed checkpoint to preserve source IDs. Preview by default;
--apply backs up the published files before replacing them.
"""
from __future__ import annotations

import argparse
import hashlib
from pathlib import Path
import re
import shutil

from arxiv_ra.evidence import TOKEN, attach_evidence
from arxiv_ra.models import ParsedPaper
from arxiv_ra.render import render_report
from arxiv_ra.report_checkpoint import file_digest, fingerprint
from arxiv_ra.utils import atomic_write_text, read_json, write_json


def refresh(directory: Path, checkpoint: Path, *, apply=False):
    metadata = read_json(directory / 'metadata.json')
    evidence = read_json(directory / 'evidence.json')
    data = read_json(checkpoint / 'parsed.json')
    if (fingerprint(data['parsed']) != data['parsed_hash'] or
            file_digest(directory / 'paper.pdf') != data['files']['paper.pdf']):
        raise ValueError('The parsed checkpoint does not match this PDF')
    original = (directory / 'report.md').read_text(encoding='utf-8')
    if evidence['numeric_audit'].get('publication_policy') == 'table_diagnostics_v2':
        print('Already refreshed:', directory.name)
        return
    citations = evidence['citations']

    def published_token(match):
        citation = next((c for c in citations if (
            c.get('source_id') == match[2].strip() if match[2] is not None else
            re.sub(r'\s+', '', c['quote']) == re.sub(r'\s+', '', match[1]))), None)
        if citation is None:
            return '（原文摘录未能唯一定位，请核对）'
        return f'[{citation["id"]}](paper.pdf#page={citation["page"]})'

    report = original
    # Match each exact previously published replacement before restoring raw
    # content. Ambiguous matches abort rather than changing the wrong passage.
    for issue in evidence['numeric_audit']['issues']:
        before = TOKEN.sub(published_token, issue['replacement'])
        if report.count(before) != 1:
            raise ValueError('Cannot uniquely restore audited block: ' + issue['original'][:80])
        report = report.replace(before, issue['original'], 1)
    report = report.split('\n## 引用与核对', 1)[0]
    report = re.sub(r'(?m)^> \*\*实验数值待核对\*\*[^\n]*\n', '', report)
    report = report.replace('（分片摘录，完整性待核对）', '（分片部分摘录）')
    by_id = {c['id']: c for c in citations}

    def restore_link(match):
        citation = by_id.get(int(match[1]))
        if citation is None or citation['page'] != int(match[2]):
            raise ValueError('Unknown source link: ' + match[0])
        return (f'[[证据ID:{citation["source_id"]}]]' if citation.get('source_id') else
                f'[[证据:{citation["quote"]}]]')

    report = re.sub(r'\[(\d+)\]\(paper\.pdf#page=(\d+)\)', restore_link, report)
    raw = data['parsed']
    parsed = ParsedPaper(text=raw['text'], page_texts=raw['page_texts'],
                         parser=raw['parser'], total_pages=raw['total_pages'])
    report, updated = attach_evidence(report, parsed, pdf_available=True,
                                      full_report=metadata['report_quality'] == 'full')
    print(directory.name, 'review items:', len(evidence['numeric_audit']['issues']), '->',
          len(updated['numeric_audit']['issues']), 'table diagnostics:',
          len(updated['numeric_audit'].get('table_diagnostics', [])))
    if not apply:
        return
    digest = hashlib.sha256(original.encode()).hexdigest()[:12]
    backup = checkpoint.parent.parent / 'table-policy-backups' / directory.name / digest
    backup.mkdir(parents=True, exist_ok=True)
    for name in ('report.md', 'report.html', 'metadata.json', 'evidence.json'):
        if not (backup / name).exists():
            shutil.copy2(directory / name, backup / name)
    metadata['evidence'] = {k: v for k, v in updated.items() if k != 'citations'}
    output_root = directory.parent.parent.parent
    render_report(report, backup / 'refreshed.html', metadata['paper']['title'],
                  arxiv_id=metadata['paper']['arxiv_id'], profile_id=metadata['profile_id'],
                  report_id=(directory / 'report.html').relative_to(output_root).as_posix())
    atomic_write_text(directory / 'report.md', report)
    write_json(directory / 'evidence.json', updated)
    write_json(directory / 'metadata.json', metadata)
    shutil.copy2(backup / 'refreshed.html', directory / 'report.html')
    print('Backup:', backup)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('report_directory', type=Path)
    parser.add_argument('checkpoint_directory', type=Path)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    refresh(args.report_directory.resolve(), args.checkpoint_directory.resolve(), apply=args.apply)
