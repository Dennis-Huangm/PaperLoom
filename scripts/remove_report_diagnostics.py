"""Back up and clean historical report diagnostics, without model requests."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from arxiv_ra.report_cleanup import clean_report_diagnostics
from arxiv_ra.render import report_document, _local_asset_base
from arxiv_ra.utils import atomic_write_text


def clean_report(path: Path, root: Path, *, apply=False):
    directory = path.parent
    names = ('report.md', 'report.html', 'metadata.json', 'evidence.json')
    original = {name: (directory / name).read_bytes() for name in names if (directory / name).is_file()}
    # Atomic text writers translate LF on Windows. Preserve backup bytes,
    # but normalize input first so CRLF is never expanded to CRCRLF.
    report = original['report.md'].decode('utf-8').replace('\r\n', '\n').replace('\r', '\n')
    metadata = json.loads(original.get('metadata.json', b'{}'))
    evidence = json.loads(original.get('evidence.json', b'{}'))
    if metadata.get('optional_citations_migration') and clean_report_diagnostics(report) == report:
        return {'path': str(path), 'status': 'unchanged'}
    audit = evidence.get('numeric_audit', {})
    if audit.get('issues') and audit.get('publication_policy') not in {
            'preserve_unverified_v1', 'table_diagnostics_v2', 'report_diagnostics_v3'}:
        # Recover old withheld values first, using saved originals only.
        from arxiv_ra.report_recovery import recover_numeric_content
        report, evidence, _ = recover_numeric_content(report, evidence)
    cleaned = clean_report_diagnostics(report)
    # All existing PDF targets outside the removed diagnostic section survive.
    citations = evidence.get('citations', [])
    evidence = {k: v for k, v in evidence.items()
                if k in {'citations', 'parsed_pages', 'total_pages', 'cited_pages', 'source_spans'}}
    evidence.update(status='located' if citations else 'unavailable', scope='source_location_only',
                    validated_citations=len(citations))
    metadata['evidence'] = {k: v for k, v in evidence.items() if k != 'citations'}
    digest = hashlib.sha256(b''.join(name.encode() + value for name, value in original.items())).hexdigest()[:16]
    backup = root / '.jobs' / 'report-cleanup' / digest
    metadata['optional_citations_migration'] = {'version': 1, 'backup': backup.relative_to(root).as_posix()}
    paper = metadata.get('paper', {})
    document = report_document(cleaned, paper.get('title', directory.name), arxiv_id=paper.get('arxiv_id', ''),
                               profile_id=metadata.get('profile_id', ''),
                               report_id=path.with_suffix('.html').relative_to(root).as_posix(),
                               asset_base=_local_asset_base(path.with_suffix('.html')))
    outputs = {'report.md': cleaned, 'report.html': document,
               'evidence.json': json.dumps(evidence, ensure_ascii=False, indent=2),
               'metadata.json': json.dumps(metadata, ensure_ascii=False, indent=2)}
    if apply:
        backup.mkdir(parents=True, exist_ok=True)
        for name, value in original.items():
            target = backup / name
            if target.exists() and target.read_bytes() != value:
                raise ValueError('Backup collision; report untouched')
            if not target.exists():
                target.write_bytes(value)
        atomic_write_text(backup / 'source.json', json.dumps({'report': str(path.resolve())}))
        if any((directory / name).read_bytes() != value for name, value in original.items()):
            raise RuntimeError('Report changed during cleanup; publication cancelled')
        try:
            for name, value in outputs.items():
                atomic_write_text(directory / name, value)
        except Exception:
            for name, value in original.items():
                (directory / name).write_bytes(value)
            for name in outputs.keys() - original.keys():
                (directory / name).unlink(missing_ok=True)
            raise
    return {'path': str(path), 'status': 'applied' if apply else 'preview',
            'backup': str(backup), 'citations': len(citations),
            'removed_characters': len(report) - len(cleaned)}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('root', type=Path, nargs='?', default=Path('run'))
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    root = args.root.resolve()
    for path in sorted(root.glob('*/reports/*/report.md')):
        print(json.dumps(clean_report(path, root, apply=args.apply), ensure_ascii=False), flush=True)
