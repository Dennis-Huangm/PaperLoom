"""Preserve extracted tables across the lossy note-to-report synthesis step."""
from __future__ import annotations

import re

from .evidence import TOKEN
from .table_quality import mask_quotes, tables


def _blocks(text):
    headings = list(re.finditer(r'(?m)^#{2,6}\s+([^\n]+)', mask_quotes(text, TOKEN)))
    groups = {}
    for row in tables(text, TOKEN):
        groups.setdefault(row['header_start'], []).append(row)
    for start, rows in groups.items():
        heading = next((h for h in reversed(headings) if h.start() < start), None)
        number = re.search(r'\bTable\s+(\d+)\b|表\s*(\d+)\b', heading[1], re.I) if heading else None
        if not number:
            continue
        context = text[heading.end():start]
        # Keep the introductory conditions; don't duplicate earlier sibling tables.
        earlier = [position for position in groups if heading.end() < position < start]
        if earlier:
            context = text[heading.end():min(earlier)]
        yield int(number[1] or number[2]), rows, text[start:rows[-1]['end']], context


def restore_note_tables(report: str, notes: list[str], inventory: list[dict]) -> str:
    """Append missing extracted rows, retaining citations and experiment context.

    This is preservation, not a claim of full PDF coverage or verified values.
    The normal citation and numeric audit runs on the restored Markdown too.
    """
    known = {item['number'] for item in inventory}

    def row_key(row):
        return tuple(re.sub(r'\s+', ' ', TOKEN.sub('', cell[0])).strip() for cell in row['cells'])

    existing = {}
    for number, rows, _, _ in _blocks(report):
        existing.setdefault(number, set()).update(row_key(row) for row in rows)
    additions = []
    for note in notes:
        for number, rows, block, context in _blocks(note):
            if number not in known:
                continue
            unseen = [row for row in rows if row_key(row) not in existing.get(number, set())]
            if not unseen:
                continue
            # Retain the table header, then append only rows absent from the
            # synthesized report and earlier note chunks.
            header = note[rows[0]['header_start']:rows[0]['start']].rstrip()
            block = header + '\n' + '\n'.join(row['raw'] for row in unseen)
            additions.append(f'### Table {number}（分片部分摘录）\n\n'
                             + context.strip() + '\n\n' + block)
            existing.setdefault(number, set()).update(row_key(row) for row in unseen)
    if not additions:
        return report
    section = re.search(r'(?m)^##\s+关键结果\s*$', report)
    following = re.search(r'(?m)^##\s+', report[section.end():]) if section else None
    end = section.end() + following.start() if following else len(report)
    content = '\n\n'.join(additions)
    if not section:
        content = '## 关键结果\n\n' + content
    return report[:end].rstrip() + '\n\n' + content + '\n\n' + report[end:]
