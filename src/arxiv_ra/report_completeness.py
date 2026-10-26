"""Preserve extracted tables in the report sections that discuss them."""
from __future__ import annotations

import re
import unicodedata
from typing import Any

from .evidence import TOKEN
from .table_extraction import (SOURCE_LINK as _LINK, SOURCE_HEADERS as _SOURCE,
                               CATEGORY_HEADERS as _CATEGORY, column_key as _column, cell_key as _cell_value,
                               text_key as _value, table_blocks as _blocks, section_headings as _headings,
                               normalize_model_groups as _normalize_unnamed_model_groups,
                               source_table_headers as _source_table_headers,
                               restore_source_header_names as _restore_source_header_names)
from .table_quality import cells, label, mask_quotes, normalize_table_separators, tables


_EXTRACTION_MISSING = {'当前材料截断', '当前材料未提供', '当前片段未提供', '当前材料缺少数值',
                       '当前材料未完全保留', '当前材料缺少可定位原文依据'}


def normalize_table_titles(report: str) -> str:
    """Remove extraction-stage labels without claiming PDF completeness.

    Completeness remains unassessed in the publication gate regardless of a
    neutral heading. Actual missing data should be explained in the discussion.
    """
    for start, end, _, title in reversed(_headings(report)):
        if not re.search(r'\bTable\s+\d+\b|表\s*\d+', title, re.I):
            continue
        heading = report[start:end]
        heading = re.sub(r'[（(](?:部分摘录|部分数据重现|具备文本引用之部分)[）)]', '', heading)
        heading = re.sub(r'(?<=\d)\s+部分数据重现', '', heading)
        report = report[:start] + heading + report[end:]
    return report


def _clean_restored_table_context(report: str) -> str:
    """Drop repeated captions and a narrow extraction-only note, never data.

    Scientific caveats, experimental settings and nonidentical captions stay.
    The removed note describes independent evidence-window coverage, not the
    source PDF's completeness; carrying it into a restored table is misleading.
    """
    for start, end, _, title in reversed(_headings(report)):
        if not re.search(r'\bTable\s+\d+\b|表\s*\d+', title, re.I):
            continue
        stop = next((h[0] for h in _headings(report) if h[0] > start), len(report))
        body = report[end:stop]
        def caption_key(value):
            value = re.sub(r'(Image-to-SVG)\s+部分(?=[）)])', r'\1', value, flags=re.I)
            return re.sub(r'\s+', ' ', value).strip().rstrip('.。')
        lines = []
        for line in body.split('\n'):
            if caption_key(line.strip()) == caption_key(title):
                continue
            # A source caption may repeat the heading, then add units or task
            # definitions. Retain those scientific details without the copy.
            extended = re.fullmatch(re.escape(title.rstrip('.。')) + r'[.。]\s+(.+)', line.strip())
            lines.append(extended[1] if extended else line)
        body = '\n'.join(lines)
        # Merging source context can repeat a metric explanation after an
        # extraction-stage sentence was removed. Compare exact paragraphs only
        # within this caption's section; different conditions and references
        # must remain, even if most of the wording is identical.
        seen_explanations = set()
        paragraphs = []
        for paragraph in re.split(r'\n\s*\n', body):
            key = paragraph.strip()
            if re.match(r'^\*{0,2}说明[：:]', key):
                if key in seen_explanations:
                    continue
                seen_explanations.add(key)
            paragraphs.append(paragraph)
        body = '\n\n'.join(paragraphs)
        report = report[:end] + body + report[stop:]
    report = re.sub(r'(?m)^\*?\(?注[：:][^\n]*(?:本片段未截(?:获|全)|独立文本片段中包含部分物理第[^\n]*(?:附属|所属)表格?主体不完整)[^\n]*\)?\*?\s*$', '', report)
    # The chunk's row count describes an extraction window, not the experiment.
    return re.sub(r'[，,]\s*片段中包含前\s*\d+\s*名模型的胜率', '', report)


def reconcile_table_availability(report: str) -> str:
    """Update a named missing-baseline note only after those rows are present.

    This establishes presence of extracted cells, not numerical correctness or
    complete PDF coverage. Unrecognized availability statements stay intact.
    """
    owners: dict[int, set[str]] = {}
    edits = []
    for number, rows, _, _ in _blocks(report):
        for row in rows:
            if row.get('category_heading_row'):
                continue
            for header, cell in zip(row['headers'], row['cells']):
                if _column(header) in {'method', 'model'}:
                    data = [c[0].strip() for h, c in zip(row['headers'], row['cells'])
                            if label(h) not in _SOURCE | _CATEGORY and _column(h) not in {'method', 'model'}]
                    if data and all(value and value not in {'—', '-', '–'} for value in data):
                        owners.setdefault(number, set()).add(_cell_value(cell[0], header))
                        # The extraction-stage note can outlive recovered data.
                        # Only remove this exact availability placeholder from
                        # the evidence cell; actual experimental cells stay put.
                        for source_header, (_, start, end) in zip(row['headers'], row['cells']):
                            if label(source_header) in _SOURCE:
                                original = report[row['start'] + start:row['start'] + end]
                                cleaned = re.sub(r'[，,]?（部分数值材料未提供）', '', original)
                                if cleaned != original:
                                    edits.append((row['start'] + start, row['start'] + end, cleaned))
    for start, end, value in sorted(edits, reverse=True):
        report = report[:start] + value + report[end:]
    pattern = re.compile(r'Table\s+(\d+)\s*中其他基线[（(]([^）)]+)[）)]完整数值在当前材料中未完全保留。', re.I)
    def replace(match):
        requested = [v.strip() for v in re.split(r'[、,，]', re.sub(r'\s*等$', '', match[2])) if v.strip()]
        available = owners.get(int(match[1]), set())
        if requested and all(any(owner == value or owner.startswith(value + '-') for owner in available) for value in requested):
            return f'Table {match[1]} 中这些基线的已提取数值见上表。'
        return match[0]
    return pattern.sub(replace, report)


def _add_sources(raw, headers, tokens):
    # Markdown permits short rows: pad omitted cells before writing evidence,
    # otherwise the source token would become part of an experimental value.
    values = [raw[begin:end].strip() for _, begin, end in cells(mask_quotes(raw, TOKEN))]
    values.extend([''] * (len(headers) - len(values)))
    index = next(i for i, header in enumerate(headers) if label(header) in _SOURCE)
    values[index] = ' '.join([values[index], *tokens]).strip()
    return '| ' + ' | '.join(values) + ' |'


def _collect_note_tables(report: str, notes: list[str], inventory: list[dict]) -> str:
    """Append missing extracted rows, retaining citations and experiment context.

    This is preservation, not a claim of full PDF coverage or verified values.
    The normal citation and numeric audit runs on the restored Markdown too.
    """
    known = {item['number'] for item in inventory}

    def row_key(row):
        values = tuple(
            (_column(header), _cell_value(cell[0], header))
            for header, cell in zip(row['headers'], row['cells'])
            if label(header) not in _SOURCE | _CATEGORY)
        if row.get('heading_qualifier'):
            values += (('__heading_condition__', row['heading_qualifier']),)
        # Repeated headers cannot bind a column by name; retain their order.
        if len({h for h, _ in values}) == len(values):
            values = tuple(sorted(values))
        return row.get('category', ''), values

    existing: dict[int, set[tuple]] = {}
    existing_rows: dict[tuple, list[dict[str, Any]]] = {}
    for number, rows, _, _ in _blocks(report):
        existing.setdefault(number, set()).update(row_key(row) for row in rows)
        for row in rows:
            existing_rows.setdefault((number, row_key(row)), []).append(row)

    def compatible_keys(left, right):
        # A category absent from the body is unknown, not evidence that its row
        # belongs to the only category currently extracted from the PDF.
        return left == right

    def merge_sources(previous, row):
        tokens = [m[0] for pattern in (TOKEN, _LINK) for m in pattern.finditer(row['raw'])
                  if m[0] not in previous['raw']]
        if tokens and any(label(h) in _SOURCE for h in previous['headers']):
            previous['raw'] = _add_sources(previous['raw'], previous['headers'], tokens)

    additions: dict[int, dict[tuple, dict[str, Any]]] = {}
    added_rows: dict[tuple, dict[str, Any]] = {}
    for note in notes:
        for number, rows, block, context in _blocks(note):
            if number not in known:
                continue
            for row in rows:
                for (old_number, key), previous_rows in existing_rows.items():
                    if old_number == number and compatible_keys(key, row_key(row)):
                        for previous in previous_rows:
                            merge_sources(previous, row)
                added = added_rows.get((number, row_key(row)))
                if added is not None:
                    # Different chunks may carry different valid source IDs.
                    # Retain all candidate IDs for the subsequent source audit.
                    merge_sources(added, row)
            unseen = [row for row in rows if not row.get('category_heading_row')
                      and not any(compatible_keys(row_key(row), key) for key in existing.get(number, set()))]
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
                schema = (tuple(label(value) for value in row['headers']), row.get('category', ''), row.get('heading_qualifier', ''))
                group = schemas.setdefault(schema, {'header': header, 'rows': [], 'contexts': [],
                                                    'category_heading': row.get('category_heading'), 'title': row['table_title']})
                group['rows'].append(row)
                if context.strip() and context.strip() not in group['contexts']:
                    group['contexts'].append(context.strip())
                for condition in row.get('condition_context', ()):
                    if not any(condition in part for part in group['contexts']):
                        group['contexts'].append(condition)
                added_rows[number, row_key(row)] = row
            existing.setdefault(number, set()).update(row_key(row) for row in unseen)
    # Apply source merges to the original text in reverse offset order; source
    # tokens may lengthen rows without shifting any earlier replacement.
    replacements = {row['start']: row for rows in existing_rows.values() for row in rows}
    for row in sorted(replacements.values(), key=lambda item: item['start'], reverse=True):
        original = report[row['start']:row['end']]
        # _blocks also fills merged task labels for comparison. Only source
        # enrichment should change an existing report's text here.
        tokens = [m[0] for pattern in (TOKEN, _LINK) for m in pattern.finditer(row['raw']) if m[0] not in original]
        if tokens and any(label(h) in _SOURCE for h in row['headers']):
            updated = _add_sources(original, row['headers'], tokens)
            report = report[:row['start']] + updated + report[row['end']:]
    if not additions:
        return report
    content = []
    for number, schemas in additions.items():
        for group in schemas.values():
            content.append('### ' + group['title'])
            content.extend(group['contexts'])
            heading = group['category_heading'] + '\n' if group['category_heading'] else ''
            content.append(group['header'] + '\n' + heading + '\n'.join(row['raw'] for row in group['rows']))
    section = ('## 原文表格摘录\n\n以下保留正文未列出的已提取数据，供查阅和核对；'
               '不表示已完整重现原表。同一表号的相同行已去重，不同数值仍保留。\n\n')
    return report.rstrip() + '\n\n' + section + '\n\n'.join(content) + '\n'


def _discussion(report, title):
    """Find a topic section before falling back to a broad result section."""
    headings = _headings(report)
    if re.search(r'comparison.*(?:benchmark|dataset)|taxonomy|paradigm|范式', title, re.I):
        root, topic = '与已有工作的区别', None
    elif re.search(r'complexity|复杂度', title, re.I):
        root, topic = '核心方法', r'复杂度|数据|关键模块|流程|complexity'
    elif re.search(r'cost|token statistics|成本|费用', title, re.I):
        root, topic = '可复现性', None
    elif re.search(r'training.*(?:data|statistic)|hyperparameter|训练数据|超参数', title, re.I):
        root, topic = '实验设置', None
    else:
        root = '关键结果'
        if re.search(r'overall.*rank|rank.*evaluation|winrate|全局.*排名|总体.*排名', title, re.I):
            topic = r'排名|胜率|rank|winrate'
        elif re.search(r'understanding|理解', title, re.I):
            topic = r'理解|understanding'
        elif re.search(r'editing|编辑', title, re.I):
            topic = r'编辑|editing'
        elif re.search(r'image-to-svg|图像到\s*SVG|图像生成', title, re.I):
            topic = r'image-to-svg|图像到\s*SVG|图像生成'
        elif re.search(r'generation|生成', title, re.I):
            topic = r'生成|generation'
        else:
            return None
    parent = next((h for h in headings if h[2] == 2 and h[3] == root), None)
    if parent is None:
        return None
    stop = next((h[0] for h in headings if h[0] > parent[0] and h[2] <= 2), len(report))
    candidates = [h for h in headings if parent[0] < h[0] < stop and h[2] == 3
                  and topic and re.search(topic, h[3], re.I)
                  and not re.match(r'^(?:Table\s*\d+|表(?:格[：:]\s*)?\s*\d+)', h[3], re.I)]
    if topic == r'生成|generation':
        candidates = [h for h in candidates if not re.search(r'排名|rank|winrate', h[3], re.I)]
    if topic == r'编辑|editing':
        candidates = [h for h in candidates if not re.search(r'失败机制|失败模式|成功与失败|定性', h[3])]
    if not candidates and root == '关键结果':
        candidates = [h for h in headings if parent[0] < h[0] < stop and h[2] == 3
                      and re.search(r'主评测|主实验|主(?:要)?结果', h[3])]
    return candidates[0] if candidates else parent


def _destination(report, number, title):
    headings = _headings(report)
    reference = re.compile(rf'\bTable\s+{number}\b|表\s*{number}(?!\d)', re.I)
    matches = [h for h in headings if reference.search(h[3])]
    # Existing table sections anchor their own footnotes. A missing table uses
    # the experiment topic, not a broad paragraph listing several table IDs.
    table_heading = [h for h in matches if re.match(r'^(?:Table\s*\d+|表(?:格[：:]\s*)?\s*(?:Table\s*)?\d+)', h[3], re.I)]
    discussion = _discussion(report, title)
    if table_heading:
        matches = table_heading
    elif discussion:
        matches = [discussion]
    if not matches:
        # A paragraph reference also anchors an omitted table to its discussion.
        for i, heading in enumerate(headings):
            end = headings[i + 1][0] if i + 1 < len(headings) else len(report)
            if reference.search(report[heading[1]:end]):
                matches.append(heading)
    if not matches:
        if re.search(r'cost|token statistics|成本|费用', title, re.I):
            target = '可复现性'
        elif re.search(r'paradigm|taxonomy|范式', title, re.I):
            target = '与已有工作的区别'
        elif re.search(r'training.*(?:data|statistic)|hyperparameter|训练数据|超参数', title, re.I):
            target = '实验设置'
        else:
            target = '关键结果'
        matches = [h for h in headings if h[3] == target]
        if not matches:
            report = report.rstrip() + f'\n\n## {target}\n\n'
            return report, len(report), 3
    heading = matches[0]
    # Source tables are siblings even if a legacy restoration nested their
    # Markdown headings. A table's notes must stop at the next numbered table,
    # not at the end of the enclosing discussion.
    def numbered_table(h):
        return re.match(r'^(?:Table\s*\d+|表\s*\d+)[：:.\s]', h[3], re.I)

    end = next((h[0] for h in headings if h[0] > heading[0]
                and (h[2] <= heading[2] or (table_heading and numbered_table(h)))), len(report))
    level = heading[2] if table_heading else max(4, min(heading[2] + 1, 6))
    return report, end, level


def _deduplicate_body_rows(report):
    """Merge exact duplicate data rows within one table, retaining all sources."""
    edits = []
    for _, rows, _, _ in _blocks(report):
        seen = {}
        for row in rows:
            if row.get('category_heading_row'):
                continue
            key = (row.get('category', ''), row.get('heading_qualifier', ''),
                   tuple((_column(h), _cell_value(c[0], h)) for h, c in zip(row['headers'], row['cells'])
                         if label(h) not in _SOURCE))
            previous = seen.get(key)
            if previous is None:
                seen[key] = row
                continue
            tokens = [m[0] for pattern in (TOKEN, _LINK) for m in pattern.finditer(row['raw'])
                      if m[0] not in previous['raw']]
            if tokens and any(label(h) in _SOURCE for h in previous['headers']):
                previous['raw'] = _add_sources(previous['raw'], previous['headers'], tokens)
            elif tokens:
                # Evidence outside a source column must not be discarded.
                continue
            edits.append((row['start'], row['end'], ''))
        edits.extend((row['start'], row['end'], row['raw']) for row in seen.values()
                     if report[row['start']:row['end']] != row['raw'])
    for start, end, raw in sorted(edits, reverse=True):
        report = report[:start] + raw + report[end:]
    return report


def _integrate_table_sections(report, titles, restored):
    """Keep table captions below topic headings and rehome legacy tail tables."""
    table_title = re.compile(r'^(?:Table\s+(\d+)\b|表(?:格[：:]\s*)?\s*(?:Table\s*)?(\d+)\b)', re.I)
    for start, end, level, title in reversed(_headings(report)):
        if level < 4 and table_title.search(title):
            report = report[:start] + '#### ' + title + '\n' + report[end:]
    numbers = [int(m[1] or m[2]) for h in _headings(report) if (m := table_title.search(h[3]))]
    for number in numbers:
        headings = _headings(report)
        heading = next((h for h in headings if (m := table_title.search(h[3])) and int(m[1] or m[2]) == number), None)
        if heading is None:
            continue
        # Previously integrated expandable tables already have a topic owner.
        if report.rfind('<details', 0, heading[0]) > report.rfind('</details>', 0, heading[0]):
            continue
        discussion_title = titles.get(number, '') + ' ' + heading[3]
        discussion = _discussion(report, discussion_title)
        if discussion:
            stop = next((h[0] for h in headings if h[0] > discussion[0] and h[2] <= discussion[2]), len(report))
            if not discussion[0] < heading[0] < stop:
                end = next((h[0] for h in headings if h[0] > heading[0] and h[2] <= heading[2]), len(report))
                section = report[heading[0]:end].strip()
                report = report[:heading[0]].rstrip() + '\n\n' + report[end:]
                discussion = _discussion(report, discussion_title)
                stop = next((h[0] for h in _headings(report) if h[0] > discussion[0] and h[2] <= discussion[2]), len(report))
                report = report[:stop].rstrip() + '\n\n' + section + '\n\n' + report[stop:]
    # Large supplemental matrices remain fully rendered, next to the analysis,
    # with a native disclosure control rather than pages of unsolicited rows.
    for start, end, level, title in reversed(_headings(report)):
        match = table_title.search(title)
        if not match or int(match[1] or match[2]) not in restored:
            continue
        stop = next((h[0] for h in _headings(report) if h[0] > start and h[2] <= level), len(report))
        section = report[start:stop].strip()
        if sum(len(rows) for _, rows, _, _ in _blocks(section)) < 15:
            continue
        number = int(match[1] or match[2])
        report = (report[:start] + f'<details class="report-table-details">\n<summary>完整模型与指标结果（Table {number}）</summary>\n\n'
                  + section + '\n\n</details>\n\n' + report[stop:])
    return report


def _merge_block(report, number, incoming):
    """Append compatible missing rows to the existing table, in its column order."""
    incoming_columns = [_column(h) for h in incoming[0]['headers'] if label(h) not in _SOURCE | _CATEGORY]
    if len(set(incoming_columns)) != len(incoming_columns):
        return None
    for old_number, existing, _, _ in _blocks(report):
        columns = [_column(h) for h in existing[0]['headers'] if label(h) not in _SOURCE | _CATEGORY]
        if old_number != number or len(set(columns)) != len(columns) or set(columns) != set(incoming_columns):
            continue
        if existing[0].get('heading_qualifier', '') != incoming[0].get('heading_qualifier', ''):
            continue
        old_groups = {_column(h) for h in existing[0]['headers'] if label(h) in _CATEGORY}
        new_groups = {_column(h) for h in incoming[0]['headers'] if label(h) in _CATEGORY}
        if max(len(old_groups), len(new_groups)) > 1 and old_groups != new_groups:
            # One implicit group cannot establish several independent axes.
            continue
        # Keep the body header and experimental cells. Add an evidence column
        # only when it is needed, so cited additions never lose their sources.
        headers = existing[0]['headers'][:]
        if (not any(label(h) in _CATEGORY for h in headers)
                and any(any(label(h) in _CATEGORY for h in r['headers']) for r in incoming)):
            headers.insert(0, next(h for r in incoming for h in r['headers'] if label(h) in _CATEGORY))
        if not any(label(h) in _SOURCE for h in headers) and any(
                TOKEN.search(r['raw']) or _LINK.search(r['raw']) for r in incoming):
            headers.append('原文依据')
        lines = ['| ' + ' | '.join(headers) + ' |', '| ' + ' | '.join(['---'] * len(headers)) + ' |']
        category = None
        for row in [*existing, *incoming]:
            if row.get('category_heading_row'):
                continue
            values = [row['raw'][a:b].strip() for _, a, b in cells(mask_quotes(row['raw'], TOKEN))]
            values += [''] * (len(row['headers']) - len(values))
            mapping = {_column(h): v for h, v in zip(row['headers'], values)}
            category_value = next((v for h, v in zip(row['headers'], values) if label(h) in _CATEGORY), '')
            if not category_value and row.get('category_heading'):
                category_value = _value(cells(row['category_heading'])[0][0])
            for h in headers:
                if label(h) in _CATEGORY:
                    mapping.setdefault(_column(h), category_value)
            sources = ' '.join(v for h, v in zip(row['headers'], values) if label(h) in _SOURCE)
            current = row.get('category', '')
            if category and not current and not any(label(h) in _CATEGORY for h in headers):
                # A flat body row has no category assertion. End the previous
                # group rather than silently assigning it that category.
                lines.append('| ' + ' | '.join(['其他', *[''] * (len(headers) - 1)]) + ' |')
            if current != category and row.get('category_heading') and not any(label(h) in _CATEGORY for h in headers):
                lines.append('| ' + ' | '.join([_value(cells(row['category_heading'])[0][0]), *[''] * (len(headers) - 1)]) + ' |')
            category = current
            lines.append('| ' + ' | '.join(sources if label(h) in _SOURCE else mapping.get(_column(h), '') for h in headers) + ' |')
        start, end = existing[0]['header_start'], existing[-1]['end']
        return report[:start] + '\n'.join(lines) + report[end:]
    return None


def _prepare_source_columns(report, notes):
    cited = {number for note in notes for number, rows, _, _ in _blocks(note)
             if any(TOKEN.search(r['raw']) or _LINK.search(r['raw']) for r in rows)}
    for number, rows, _, _ in reversed(list(_blocks(report))):
        if number not in cited or any(label(h) in _SOURCE for h in rows[0]['headers']):
            continue
        lines = ['| ' + ' | '.join([*rows[0]['headers'], '原文依据']) + ' |',
                 '| ' + ' | '.join(['---'] * (len(rows[0]['headers']) + 1)) + ' |']
        for row in rows:
            values = [row['raw'][begin:end].strip() for _, begin, end in row['cells']]
            lines.append('| ' + ' | '.join([*values, '']) + ' |')
        report = report[:rows[0]['header_start']] + '\n'.join(lines) + report[rows[-1]['end']:]
    return report


def _fill_extraction_placeholders(report, notes, inventory):
    """Fill explicit truncation markers only from a uniquely matching note row.

    Bind the table, schema, row label, category, heading condition and every
    retained cell. Conflicting candidates and ordinary missing-value dashes
    stay untouched; no model identity or numerical similarity is inferred.
    """
    missing = _EXTRACTION_MISSING
    known = {item['number'] for item in inventory}
    candidates = {}
    def data(row):
        return {_column(h): (v, h) for h, (v, _, _) in zip(row['headers'], row['cells'])
                if label(h) not in _SOURCE | _CATEGORY}
    for note in notes:
        for number, rows, _, _ in _blocks(note):
            if number in known:
                candidates.setdefault(number, []).extend(r for r in rows if not r.get('category_heading_row'))
    edits = []
    owners = {'model', 'method', 'dataset', 'statistic', 'task', 'generator', 'benchmark', 'metric'}
    for number, rows, _, _ in _blocks(report):
        for row in rows:
            values = data(row)
            if len(values) != sum(label(h) not in _SOURCE | _CATEGORY for h in row['headers']):
                continue
            absent = {h for h, (v, _) in values.items() if _value(v) in missing}
            identifiers = {h for h in values if h in owners and h not in absent and _value(values[h][0])}
            if not absent or not identifiers:
                continue
            matches = {}
            for candidate in candidates.get(number, []):
                other = data(candidate)
                if (len(other) != sum(label(h) not in _SOURCE | _CATEGORY for h in candidate['headers'])
                        or set(other) != set(values) or row.get('category', '') != candidate.get('category', '')
                        or row.get('heading_qualifier', '') != candidate.get('heading_qualifier', '')):
                    continue
                if any(_cell_value(values[h][0], values[h][1]) != _cell_value(other[h][0], other[h][1])
                       for h in values if h not in absent):
                    continue
                if any(not _value(other[h][0]) or _value(other[h][0]) in missing for h in absent):
                    continue
                key = tuple(sorted((h, _cell_value(other[h][0], other[h][1])) for h in absent))
                matches.setdefault(key, other)
            if len(matches) != 1:
                continue
            other = next(iter(matches.values()))
            raw = row['raw']
            replacements = [(a, b, other[_column(h)][0]) for h, (_, a, b) in zip(row['headers'], row['cells'])
                            if _column(h) in absent]
            for a, b, value in sorted(replacements, reverse=True):
                raw = raw[:a] + ' ' + value.strip() + ' ' + raw[b:]
            edits.append((row['start'], row['end'], raw))
    for start, end, raw in sorted(edits, reverse=True):
        report = report[:start] + raw + report[end:]
    return report


def _physical_row_group(number, row, pages):
    """Confirm the complete row and its Open/Closed group in a physical page."""
    def physical(value):
        value = unicodedata.normalize('NFKC', _value(value))
        value = value.replace(r'\times', '×').replace(r'\(', '').replace(r'\)', '')
        return re.sub(r'[\s|${}^]+', '', value).casefold()
    values = [v[0] for h, v in zip(row['headers'], row['cells']) if label(h) not in _SOURCE | _CATEGORY
              and _cell_value(v[0], h) != '-']
    needle = physical(''.join(values))
    if not needle or not any(re.search(r'\d', value) for value in values[1:]):
        return None
    found = set()
    for page in pages:
        if not re.search(rf'\bTable\s+{number}\s*[:.]', page, re.I):
            continue
        markers = [(len(physical(page[:m.start()])), m[1].casefold())
                   for m in re.finditer(r'(?m)^\s*(Open|Closed)\s*$', page)]
        if not markers:
            continue
        source = physical(page)
        for match in re.finditer(re.escape(needle), source):
            found.add(next((group for offset, group in reversed(markers) if offset < match.start()), ''))
    return next(iter(found)) if len(found) == 1 else None


def _anchor_body_model_groups(report, notes, pages):
    """Enrich flat rows only from unique source-anchored note rows.

    All retained cells, model variants, schemas and heading conditions bind the
    match. A group omitted from a report is not inferred from notes alone.
    """
    candidates = {}
    def data(row):
        return {_column(h): _cell_value(c[0], h) for h, c in zip(row['headers'], row['cells'])
                if label(h) not in _SOURCE | _CATEGORY}
    for note in notes:
        for number, rows, _, _ in _blocks(note):
            for row in rows:
                if row.get('category_heading_row') or not any(label(h) == 'model category' for h in row['headers']):
                    continue
                if len(data(row)) != sum(label(h) not in _SOURCE | _CATEGORY for h in row['headers']):
                    continue
                group = _physical_row_group(number, row, pages)
                if group is not None and group == row.get('category', ''):
                    candidates.setdefault(number, []).append((row, data(row), group))
    restored = set()
    for number, rows, _, _ in reversed(list(_blocks(report))):
        source = [(row, values, group) for row, values, group in candidates.get(number, [])
                  if row.get('heading_qualifier', '') == rows[0].get('heading_qualifier', '')
                  and set(values) == set(data(rows[0]))]
        if not source or any(label(h) in _CATEGORY for h in rows[0]['headers']):
            continue
        if any(len(data(row)) != sum(label(h) not in _SOURCE | _CATEGORY for h in row['headers']) for row in rows):
            continue
        groups = []
        for row in rows:
            values = data(row)
            matches = {group for other, cells, group in source
                       if set(cells) == set(values) and row.get('heading_qualifier', '') == other.get('heading_qualifier', '')
                       and all(value in _EXTRACTION_MISSING or value == cells[h] for h, value in values.items())}
            groups.append(next(iter(matches)) if len(matches) == 1 else None)
        if any(group is None for row, group in zip(rows, groups)
               if not re.search(r'等其余\s*\d+\s*个模型', row['cells'][0][0])):
            continue
        # Remove a synthetic "remaining N models" placeholder only after the
        # PDF confirms precisely those missing model configurations in notes.
        def owner_key(cells):
            return tuple((h, v) for h, v in sorted(cells.items()) if h in {'model', 'method', '#params'})
        all_owners = {owner_key(cells) for _, cells, group in source if group}
        body_owners = {owner_key(data(row)) for row, group in zip(rows, groups) if group is not None}
        remaining = len(all_owners - body_owners)
        lines = ['| ' + ' | '.join(['Model Category', *rows[0]['headers']]) + ' |',
                 '| ' + ' | '.join(['---'] * (len(rows[0]['headers']) + 1)) + ' |']
        for row, group in zip(rows, groups):
            match = re.search(r'等其余\s*(\d+)\s*个模型', row['cells'][0][0])
            values = data(row)
            payload = [v for h, v in values.items() if h not in {'model', 'method', 'type', '#params'}]
            if match and int(match[1]) == remaining and payload and all(v in _EXTRACTION_MISSING for v in payload):
                continue
            raw = [row['raw'][a:b].strip() for _, a, b in row['cells']]
            lines.append('| ' + ' | '.join([group.title() if group else '', *raw]) + ' |')
        report = report[:rows[0]['header_start']] + '\n'.join(lines) + report[rows[-1]['end']:]
        restored.add(number)
    return report, restored


def _clean_recovered_model_notes(report, numbers):
    for start, end, level, title in reversed(_headings(report)):
        match = re.search(r'\bTable\s+(\d+)\b', title, re.I)
        if not match or int(match[1]) not in numbers:
            continue
        stop = next((h[0] for h in _headings(report) if h[0] > start and h[2] <= level), len(report))
        body = report[end:stop]
        if any(_value(c[0]) in _EXTRACTION_MISSING for row in tables(body, TOKEN)
               for h, c in zip(row['headers'], row['cells']) if label(h) not in _SOURCE):
            continue
        body = re.sub(r'(?m)^\*\(注[：:][^\n]*由于分片取回[^\n]*主表中作缺失说明[^\n]*$', '', body)
        body = re.sub(r'当前材料中部分[^。\n]*数据行未在有效证据片段中重现数值[^。\n]*。', '', body)
        report = report[:end] + body + report[stop:]
    return report


def restore_note_tables(report: str, notes: list[str], inventory: list[dict], *, source_pages=(), integrate=True) -> str:
    """Fill body tables and place omitted tables beside their discussion.

    Legacy appendices are treated as extracted notes, making retries and stored
    reports use the same preservation path. No values or conditions are guessed.
    With integrate=False this is an extraction adapter: presentation wrappers
    and topic placement are left to the catalogue renderer.
    """
    # A short separator must be repaired before discovery, otherwise an existing
    # body table appears absent and is restored a second time from the notes.
    report = normalize_table_separators(report, TOKEN)
    original_numbers = {b[0] for b in _blocks(report)}
    notes = [normalize_table_separators(note, TOKEN) for note in notes]
    report = _normalize_unnamed_model_groups(report)
    notes = [_normalize_unnamed_model_groups(note) for note in notes]
    source_headers = _source_table_headers(source_pages)
    report = _restore_source_header_names(report, source_headers)
    notes = [_restore_source_header_names(note, source_headers) for note in notes]
    legacy = next((h for h in _headings(report) if h[3] == '原文表格摘录'), None)
    if legacy:
        end = next((h[0] for h in _headings(report) if h[0] > legacy[0] and h[2] <= 2), len(report))
        legacy_note = report[legacy[1]:end]
        notes = [legacy_note, *notes]
        # An old directory may be incomplete. Preserve saved tables even when
        # the current inventory cannot identify them; coverage remains separate.
        known = {item['number'] for item in inventory}
        inventory = [*inventory, *({'number': n} for n in sorted({b[0] for b in _blocks(legacy_note)} - known))]
        report = report[:legacy[0]].rstrip() + '\n\n' + report[end:]
    report, recovered_groups = _anchor_body_model_groups(report, notes, source_pages)
    report = _fill_extraction_placeholders(report, notes, inventory)
    report = _prepare_source_columns(report, notes)
    collected = _collect_note_tables(report, notes, inventory)
    appendix = next((h for h in _headings(collected) if h[3] == '原文表格摘录'), None)
    report = collected[:appendix[0]].rstrip() + '\n' if appendix else collected
    additions = collected[appendix[1]:] if appendix else ''
    titles = {item['number']: item.get('title', '') for item in inventory}
    for number, rows, block, context in _blocks(additions):
        merged = _merge_block(report, number, rows)
        if merged is not None:
            report = merged
            # Keep source experiment conditions near the merged body table.
            if context.strip() and context.strip() not in report:
                report, position, _ = _destination(report, number, titles.get(number, ''))
                report = report[:position].rstrip() + '\n\n' + context.strip() + '\n\n' + report[position:]
            continue
        report, position, level = _destination(report, number, titles.get(number, ''))
        text = ('#' * level + ' ' + rows[0]['table_title'] + '\n\n' + context.strip() + '\n\n' + block.strip() + '\n\n')
        report = report[:position].rstrip() + '\n\n' + text + report[position:]
    # Row deduplication must not discard captions, conditions or footnotes
    # carried by a duplicate extraction. Retain those beside the body table.
    present = {b[0] for b in _blocks(report)}
    for note in notes:
        blocks = list(_blocks(note))
        headings = _headings(note)
        for number, rows, _, context in blocks:
            if number not in present:
                continue
            end = min([len(note), *[h[0] for h in headings if h[0] > rows[-1]['end']],
                       *[r[0]['header_start'] for _, r, _, _ in blocks if r[0]['header_start'] > rows[-1]['end']]])
            paragraphs = re.split(r'\n\s*\n', context.strip() + '\n\n' + note[rows[-1]['end']:end].strip())
            for paragraph in paragraphs:
                paragraph = paragraph.strip()
                if not paragraph or paragraph in report:
                    continue
                report, position, _ = _destination(report, number, titles.get(number, ''))
                report = report[:position].rstrip() + '\n\n' + paragraph + '\n\n' + report[position:]
    report = _deduplicate_body_rows(report)
    report = _clean_recovered_model_notes(report, recovered_groups)
    report = reconcile_table_availability(_clean_restored_table_context(normalize_table_titles(report)))
    return _integrate_table_sections(report, titles, present - original_numbers) if integrate else report
