"""Preserve extracted tables across the lossy note-to-report synthesis step."""
from __future__ import annotations

import re

from .evidence import TOKEN
from .table_quality import cells, label, mask_quotes, tables


def _value(text):
    # Presentation and evidence are not experimental data. Do not normalise
    # numbers, units, model names or conditions when comparing rows.
    text = TOKEN.sub('', text)
    text = re.sub(r'\[\d+\]\(paper\.pdf#page=\d+\)', '', text)
    text = re.sub(r'\*\*([^*]+)\*\*|`([^`]+)`', lambda m: m[1] or m[2], text)
    return re.sub(r'\s+', ' ', text).strip()


def _add_sources(raw, headers, tokens):
    # Markdown permits short rows: pad omitted cells before writing evidence,
    # otherwise the source token would become part of an experimental value.
    values = [raw[begin:end].strip() for _, begin, end in cells(mask_quotes(raw, TOKEN))]
    values.extend([''] * (len(headers) - len(values)))
    values[-1] = ' '.join([values[-1], *tokens]).strip()
    return '| ' + ' | '.join(values) + ' |'


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
        # Task tables commonly use a merged first column. Make its meaning
        # explicit before deduplication; identical scores in different tasks
        # must never collapse into one row.
        task = ''
        category = ''
        category_heading = None
        category_column = label(rows[0]['headers'][0]) in {'模型分类', 'model category'}
        for row in rows:
            first = _value(row['cells'][0][0])
            if category_column:
                row['category'] = first.casefold()
            elif first and all(not _value(cell[0]) for cell in row['cells'][1:]):
                category = re.sub(r'\s+models$', '', first, flags=re.I).casefold()
                category_heading = row['raw']
                row['category_heading_row'] = True
            elif category:
                row['category'] = category
                row['category_heading'] = category_heading
        if label(rows[0]['headers'][0]) in {'task', '任务'}:
            for row in rows:
                value, begin, finish = row['cells'][0]
                if value.strip():
                    task = value
                elif task:
                    row['cells'][0] = (task, begin, finish)
                    row['raw'] = row['raw'][:begin] + ' ' + task + ' ' + row['raw'][finish:]
        yield int(number[1] or number[2]), rows, text[start:rows[-1]['end']], context


def restore_note_tables(report: str, notes: list[str], inventory: list[dict]) -> str:
    """Append missing extracted rows, retaining citations and experiment context.

    This is preservation, not a claim of full PDF coverage or verified values.
    The normal citation and numeric audit runs on the restored Markdown too.
    """
    known = {item['number'] for item in inventory}

    def row_key(row):
        return (row.get('category', ''), tuple(
            (re.sub(r'[\s:：]+', '', label(header)), _value(cell[0]))
            for header, cell in zip(row['headers'], row['cells'])
            if label(header) not in {'依据', '原文依据', '模型分类', 'model category'}))

    existing = {}
    existing_rows = {}
    for number, rows, _, _ in _blocks(report):
        existing.setdefault(number, set()).update(row_key(row) for row in rows)
        for row in rows:
            existing_rows.setdefault((number, row_key(row)), []).append(row)

    def merge_sources(previous, row):
        tokens = [m[0] for m in TOKEN.finditer(row['raw']) if m[0] not in previous['raw']]
        if tokens and label(previous['headers'][-1]) in {'依据', '原文依据'}:
            previous['raw'] = _add_sources(previous['raw'], previous['headers'], tokens)

    additions = {}
    added_rows = {}
    for note in notes:
        for number, rows, block, context in _blocks(note):
            if number not in known:
                continue
            for row in rows:
                for previous in existing_rows.get((number, row_key(row)), []):
                    merge_sources(previous, row)
                previous = added_rows.get((number, row_key(row)))
                if previous is not None:
                    # Different chunks may carry different valid source IDs.
                    # Retain all candidate IDs for the subsequent source audit.
                    merge_sources(previous, row)
            unseen = [row for row in rows if not row.get('category_heading_row')
                      and row_key(row) not in existing.get(number, set())]
            if not unseen:
                continue
            # Retain the table header, then append only rows absent from the
            # synthesized report and earlier note chunks.
            header = note[rows[0]['header_start']:rows[0]['start']].rstrip()
            # Consolidate only compatible column layouts. Different layouts
            # and conflicting values remain visible, never guessed or averaged.
            schemas = additions.setdefault(number, {})
            for row in unseen:
                # Each category has its own table, so deduplicating a repeated
                # heading cannot move later models beneath another category.
                schema = (tuple(label(value) for value in row['headers']), row.get('category', ''))
                group = schemas.setdefault(schema, {'header': header, 'rows': [], 'contexts': [],
                                                    'category_heading': row.get('category_heading')})
                group['rows'].append(row)
                if context.strip() and context.strip() not in group['contexts']:
                    group['contexts'].append(context.strip())
                added_rows[number, row_key(row)] = row
            existing.setdefault(number, set()).update(row_key(row) for row in unseen)
    # Apply source merges to the original text in reverse offset order; source
    # tokens may lengthen rows without shifting any earlier replacement.
    replacements = {row['start']: row for rows in existing_rows.values() for row in rows}
    for row in sorted(replacements.values(), key=lambda item: item['start'], reverse=True):
        original = report[row['start']:row['end']]
        # _blocks also fills merged task labels for comparison. Only source
        # enrichment should change an existing report's text here.
        tokens = [m[0] for m in TOKEN.finditer(row['raw']) if m[0] not in original]
        if tokens and label(row['headers'][-1]) in {'依据', '原文依据'}:
            updated = _add_sources(original, row['headers'], tokens)
            report = report[:row['start']] + updated + report[row['end']:]
    if not additions:
        return report
    content = []
    for number, schemas in additions.items():
        content.append(f'### Table {number}（部分摘录）')
        for group in schemas.values():
            content.extend(group['contexts'])
            heading = group['category_heading'] + '\n' if group['category_heading'] else ''
            content.append(group['header'] + '\n' + heading + '\n'.join(row['raw'] for row in group['rows']))
    section = ('## 原文表格摘录\n\n以下保留正文未列出的已提取数据，供查阅和核对；'
               '不表示已完整重现原表。同一表号的相同行已去重，不同数值仍保留。\n\n')
    return report.rstrip() + '\n\n' + section + '\n\n'.join(content) + '\n'
