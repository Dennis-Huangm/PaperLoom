"""Recheck a frozen report and optionally retry failed Chinese table notes.

The PDF, extracted matrices, figures and prose are retained. Model calls only
edit table captions/notes. Existing outputs are backed up before any changes.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re

from arxiv_ra.evidence import attach_evidence
from arxiv_ra.models import ParsedPaper
from arxiv_ra.quality import normalized_excerpt
from arxiv_ra.render import _local_asset_base, report_document
from arxiv_ra.report_completeness import _headings
from arxiv_ra.report_checkpoint import CheckpointWriteError
from arxiv_ra.report_tables import ReportTables
from arxiv_ra.source_spans import exact_span, source_spans
from arxiv_ra.utils import atomic_write_text, read_json, write_json


OUTPUTS = ('report.md', 'report.html', 'tables.json', 'evidence.json',
           'table-preservation.json', 'metadata.json')


def repair(directory, parsed_file, *, chat=None, apply=False, editorials_file=None):
    directory = Path(directory).resolve()
    original = {name: (directory / name).read_text('utf-8') for name in OUTPUTS}
    digest = hashlib.sha256(json.dumps(original, sort_keys=True).encode()).hexdigest()[:16]
    backup = directory / 'annotation-repair' / digest
    backup.mkdir(parents=True, exist_ok=True)
    for name, content in original.items():
        if not (backup / name).exists():
            atomic_write_text(backup / name, content)
    metadata = json.loads(original['metadata.json'])
    old_evidence = json.loads(original['evidence.json'])
    catalogue = ReportTables.from_dict(json.loads(original['tables.json']))
    raw = read_json(Path(parsed_file))['parsed']
    parsed = ParsedPaper(raw['text'], raw['page_texts'], total_pages=raw['total_pages'])
    spans = source_spans(parsed)
    citations = {str(c['id']): c for c in old_evidence['citations']}
    checked = {}
    for identifier, c in citations.items():
        key = c.get('source_id')
        span = (exact_span(key, parsed) or spans.get(key)) if key else None
        if (not span or span['page'] != c['page']
                or normalized_excerpt(span['quote']) != normalized_excerpt(c['quote'])):
            raise ValueError('Parsed checkpoint does not match the stored citation')
        checked[identifier] = key

    def source(match):
        c = citations.get(match[1])
        if not c or c['page'] != int(match[2]):
            raise ValueError('Stored citation does not match the PDF link')
        return f'[[证据ID:{checked[match[1]]}]]'

    # Remove only generated evidence sections/notices; keep the existing prose
    # and managed table positions rather than regenerating the report body.
    prose = catalogue.prose(original['report.md'])
    headings = _headings(prose)
    for h in reversed(headings):
        if h[3] in {'引用与核对', '原文依据与覆盖'}:
            end = next((x[0] for x in headings if x[0] > h[0] and x[2] <= h[2]), len(prose))
            prose = prose[:h[0]] + prose[end:]
    prose = re.sub(r'(?m)^> \*\*数值引用存在冲突\*\*[^\n]*\n?', '', prose)
    prose = re.sub(r'\[(\d+)\]\(paper\.pdf#page=(\d+)(?: "[^"]*")?\)', source, prose)
    updated = catalogue
    if editorials_file is not None:
        updated = ReportTables.from_dict(read_json(Path(editorials_file)))
        if (updated.tables != catalogue.tables or updated.annotations != catalogue.annotations
                or updated.source_resolutions != catalogue.source_resolutions):
            raise ValueError('Proposed annotations changed frozen source content')
    if chat is not None:
        def cached_chat(stage, system, prompt):
            key = hashlib.sha256(json.dumps([stage, system, prompt]).encode()).hexdigest()
            # Report slugs are already long on Windows; keep basenames short.
            path = backup / 'calls' / f'{key[:16]}.json'
            saved = read_json(path)
            if saved and saved.get('signature') == key:
                return saved['text']
            print(stage, flush=True)
            result = chat(stage, system, prompt)
            try:
                write_json(path, {'signature': key, 'stage': stage, 'text': result})
            except OSError as exc:
                raise CheckpointWriteError('Cannot save annotation repair checkpoint') from exc
            return result
        updated = updated.localize_annotations(cached_chat)
    if updated.tables != catalogue.tables:
        raise ValueError('Repair changed frozen source tables')
    report, evidence = attach_evidence(updated.render(prose), parsed,
                                      pdf_available=(directory / 'paper.pdf').is_file(),
                                      full_report=metadata.get('report_quality') == 'full')
    review = updated.review(report)
    if review['status'] != 'passed':
        raise ValueError('Repaired report failed table preservation: ' + json.dumps(review, ensure_ascii=False))
    evidence['table_preservation'] = review
    evidence.setdefault('table_coverage', {})['source_displays'] = old_evidence.get('table_coverage', {}).get('source_displays', [])
    root = directory.parents[2]
    html = report_document(report, metadata['paper']['title'], arxiv_id=metadata['paper']['arxiv_id'],
                           profile_id=metadata['profile_id'],
                           report_id=(directory / 'report.html').relative_to(root).as_posix(),
                           asset_base=_local_asset_base(directory / 'report.html'))
    metadata['evidence'] = {k: v for k, v in evidence.items() if k != 'citations'}
    metadata['annotation_repair'] = {'backup': backup.relative_to(directory).as_posix(),
                                     'scope': 'table_editorials_and_source_links'}
    outputs = {'report.md': report, 'report.html': html}
    for name, data in [('tables.json', updated.to_dict()), ('evidence.json', evidence),
                       ('metadata.json', metadata), ('table-preservation.json', review)]:
        outputs[name] = json.dumps(data, ensure_ascii=False, indent=2)
    # Always stage and validate first. Concurrent publication must not be lost.
    for name, content in outputs.items():
        atomic_write_text(backup / 'preview' / name, content)
    if apply:
        if any((directory / n).read_text('utf-8') != text for n, text in original.items()):
            raise RuntimeError('Report changed during repair; preview retained, publication cancelled')
        try:
            for name, content in outputs.items():
                atomic_write_text(directory / name, content)
        except Exception:
            for name, content in original.items():
                atomic_write_text(directory / name, content)
            raise
    summary = {'applied': apply, 'backup': str(backup), 'preservation': review,
               'localized': sum(s['status'] == 'localized' for s in updated.editorial_status),
               'remaining': [s for s in updated.editorial_status if s['status'] != 'localized'],
               'flagged_cells': 0}
    write_json(backup / 'result.json', summary)
    return summary


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('directory', type=Path)
    parser.add_argument('--parsed', type=Path, required=True)
    parser.add_argument('--localize', action='store_true')
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--editorials', type=Path, help='Validated tables.json from an earlier repair preview')
    args = parser.parse_args()
    chat = None
    if args.localize:
        from arxiv_ra.config import load_config
        from arxiv_ra.llm import LLMClient
        from scripts.rebuild_reports import _load_dotenv
        _load_dotenv(Path('.env'))
        llm = LLMClient(load_config(Path('config.yaml')).llm)
        chat = lambda stage, system, prompt: llm.chat(system, prompt)
    print(json.dumps(repair(args.directory, args.parsed, chat=chat, apply=args.apply,
                           editorials_file=args.editorials), ensure_ascii=False, indent=2))
