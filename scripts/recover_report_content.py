"""Preview or apply reversible recovery of legacy numeric-audit data loss."""
import argparse
import hashlib
from pathlib import Path
import shutil

from arxiv_ra.render import render_report
from arxiv_ra.report_recovery import recover_numeric_content
from arxiv_ra.utils import atomic_write_text, read_json, write_json


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('root', type=Path)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    root = args.root.resolve()
    for path in sorted(root.glob('*/reports/*/evidence.json')):
        directory = path.parent
        original = (directory / 'report.md').read_text(encoding='utf-8')
        report, evidence, stats = recover_numeric_content(original, read_json(path))
        if report == original:
            continue
        print(directory.name, stats)
        if not args.apply:
            continue
        digest = hashlib.sha256(original.encode()).hexdigest()[:12]
        backup = root / '.jobs' / 'content-recovery-backups' / directory.name / digest
        backup.mkdir(parents=True, exist_ok=True)
        for name in ('report.md', 'report.html', 'metadata.json', 'evidence.json'):
            destination = backup / name
            if (directory / name).is_file() and not destination.exists():
                shutil.copy2(directory / name, destination)
        metadata = read_json(directory / 'metadata.json') if (directory / 'metadata.json').is_file() else None
        if metadata:
            metadata['evidence'] = evidence
            render_report(report, backup / 'recovered.html', metadata['paper']['title'],
                          arxiv_id=metadata['paper']['arxiv_id'], profile_id=metadata['profile_id'],
                          report_id=(directory / 'report.html').relative_to(root).as_posix())
        atomic_write_text(directory / 'report.md', report)
        write_json(path, evidence)
        if metadata:
            write_json(directory / 'metadata.json', metadata)
            (backup / 'recovered.html').replace(directory / 'report.html')


if __name__ == '__main__':
    main()
