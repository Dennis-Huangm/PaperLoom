"""Resolve competing extractions only when a whole matrix occurs in the PDF.

This runs before freezing cells. It is deliberately conservative: matching a
number somewhere on a page, a majority vote, or model confidence is not proof.
Original edits and the supporting physical text are retained in the catalogue.
"""
from __future__ import annotations

from collections import defaultdict
import re

from .evidence import TOKEN
from .report_completeness import _blocks, _headings, _column, _LINK, _SOURCE
from .table_quality import label


def _plain(value):
    value = _LINK.sub('', TOKEN.sub('', value))
    return ' '.join(value.replace('**', '').split())


def _regions(pages):
    regions = defaultdict(list)
    for page_number, text in enumerate(pages, 1):
        captions = list(re.finditer(r'(?im)^\s*Table\s+(\d+)\s*[:.]', text))
        for i, caption in enumerate(captions):
            end = captions[i + 1].start() if i + 1 < len(captions) else len(text)
            regions[int(caption[1])].append((page_number, text[caption.end():end]))
    return regions


def _reversed_source_claim(clause, differences):
    """Recognise a disproven transcription claim, not scientific footnotes."""
    if not re.search(r'原表|原文|PDF|Markdown|解析', clause):
        return False
    return any(re.search(r'(?:为|标为|记为)\s*' + re.escape(rejected)
                         + r'(?![\d.]).*?(?:误作|误识别为|错作)\s*'
                         + re.escape(accepted) + r'(?![\d.])', clause)
               for rejected, accepted in differences)


def apply_source_resolutions(notes, resolutions):
    result = list(notes)
    for resolution in resolutions:
        for edit in resolution['edits']:
            index = edit['note_index']
            # The synthesis input may have different citation decorations.
            # Never apply an edit to another occurrence or another note.
            if index < len(result) and result[index].count(edit['before']) == 1:
                result[index] = result[index].replace(edit['before'], edit['after'], 1)
    return result


def reconcile_source_matrices(notes, pages):
    groups = defaultdict(list)
    for note_index, note in enumerate(notes):
        for number, rows, _, context in _blocks(note):
            indices = [i for i, h in enumerate(rows[0]['headers']) if label(h) not in _SOURCE]
            if len(indices) < 3 or len(rows) < 2:
                continue
            values = tuple(tuple(_plain(row['cells'][i][0]) for i in indices) for row in rows)
            # Same schema, row labels, group boundaries and explicit condition.
            # Repeated method names under different datasets remain distinct.
            title_condition = tuple(re.findall(r'[（(][^）)]+[）)]', rows[0]['table_title']))
            key = (number, title_condition, _plain(context), tuple(_column(rows[0]['headers'][i]) for i in indices),
                   tuple((row.get('heading_qualifier', ''), row.get('category', ''),
                          bool(row.get('category_heading_row')), value[0])
                         for row, value in zip(rows, values)))
            groups[key].append((note_index, rows, indices, values))
    regions = _regions(pages)
    resolutions = []
    for key, candidates in groups.items():
        number = key[0]
        distinct = {item[3] for item in candidates}
        if len(distinct) < 2 or len(regions[number]) != 1:
            continue
        page_number, physical = regions[number][0]
        physical = ' '.join(physical.split())
        supported = []
        for matrix in distinct:
            text = ' '.join(value for row in matrix for value in row if value)
            # Whole cells in order, contiguous and token bounded. No numeric
            # normalisation, gap skipping, empty-cell filling or column guessing.
            if re.search(r'(?<!\S)' + re.escape(text) + r'(?!\S)', physical):
                supported.append(matrix)
        if len(supported) != 1:
            continue
        selected = supported[0]
        differences = [(a, b) for matrix in distinct for row, chosen in zip(matrix, selected)
                       for a, b in zip(row, chosen) if a != b]
        # A changed label/blank/condition is not an alternative scalar reading.
        scalar = re.compile(r'[+−-]?\d+(?:[.,]\d+)*(?:%|[KMB])?')
        if any(not scalar.fullmatch(a) or not scalar.fullmatch(b) for a, b in differences):
            continue
        edits = []
        for note_index, rows, indices, matrix in candidates:
            for row, values, chosen in zip(rows, matrix, selected):
                if values == chosen:
                    continue
                raw = row['raw']
                replacements = [(row['cells'][i][1], row['cells'][i][2], target)
                                for i, value, target in zip(indices, values, chosen) if value != target]
                for start, end, target in sorted(replacements, reverse=True):
                    raw = raw[:start] + ' ' + target + ' ' + raw[end:]
                edits.append({'note_index': note_index, 'before': row['raw'], 'after': raw})
            if matrix == selected:
                continue
            # Drop only stale parser-correction claims that mention BOTH the
            # rejected and accepted scalar. Preserve other footnote clauses.
            note = notes[note_index]
            heading = max((h for h in _headings(note) if h[0] < rows[0]['header_start']),
                          default=(0, 0, 0, ''))
            stop = next((h[0] for h in _headings(note) if h[0] > rows[-1]['end']), len(note))
            for paragraph in re.split(r'\n\s*\n', note[heading[1]:stop]):
                if '|' in paragraph or not re.search(r'误作|误识别为|错作', paragraph):
                    continue
                clauses = re.split(r'(?<=[；;。])', paragraph)
                retained = [clause for clause in clauses if not _reversed_source_claim(clause, differences)]
                if retained != clauses:
                    remainder = ''.join(retained).strip().strip('*').strip()
                    edits.append({'note_index': note_index, 'before': paragraph, 'after': remainder})
        if edits:
            resolutions.append({'table': number, 'source_page': page_number,
                                'basis': 'unique_contiguous_source_matrix',
                                'source_matrix': ' '.join(v for row in selected for v in row if v),
                                'edits': edits})
    return apply_source_resolutions(notes, resolutions), tuple(resolutions)
