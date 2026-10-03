"""Freeze extracted tables before synthesis and render each source table once.

Legacy Markdown extraction is an ingestion adapter only. Prose synthesis and
citation repair never supply, merge or edit the catalogue's experimental cells.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, replace
import hashlib
import json
import re

from .evidence import TOKEN
from .report_completeness import (_blocks, _headings, _destination, _SOURCE, _column,
                                  _cell_value, _clean_recovered_model_notes,
                                  restore_note_tables, _LINK)
from .table_quality import label
from .table_schema import column_role
from .table_layouts import reconcile_layouts
from .table_annotations import (annotation_units, display_annotations, validate_groups,
                                ANNOTATION_SYSTEM, ANNOTATION_PROMPT)
from .utils import extract_json_object
from .model_budget import ModelBudgetExceeded
from .report_checkpoint import CheckpointWriteError
from .table_source_reconciliation import reconcile_source_matrices, apply_source_resolutions


_SLOT = re.compile(r'(?m)^[ \t]*\[\[表格:(table-\d+)(?:\|(展开|折叠))?\]\][ \t]*$')
_MANAGED = re.compile(r'<!-- paperloom-table:(table-\d+):start -->\n.*?\n<!-- paperloom-table:\1:end -->', re.S)


def _digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def _table_context(paragraphs):
    """Retain table notes; extraction analysis belongs to synthesis prose.

    Legacy notes may append a labelled analysis and its bullet list to a
    matrix. It remains in synthesis_notes, but is not a table footnote to
    display again alongside the model's body analysis.
    """
    analysis = False
    retained = []
    for paragraph in paragraphs:
        heading = re.fullmatch(r'\*\*([^\n]+?)\*\*\s*[：:]?', paragraph.splitlines()[0])
        if heading and re.search(r'(?:结果|结论|观察|趋势).*(?:归纳|总结|分析)|主要发现', heading[1]):
            analysis = True
            continue
        if analysis and re.match(r'(?:[-+*]\s+|\d+[.)]\s+)', paragraph):
            continue
        analysis = False
        retained.append(paragraph)
    return tuple(retained)


def _sub_directives(pattern, replace, text):
    """Only interpret directives outside fenced examples and source quotes."""
    masked = TOKEN.sub(lambda m: 'X' * len(m[0]), text)
    fenced = []
    fence, start, offset = None, 0, 0
    for line in masked.splitlines(keepends=True):
        marker = re.match(r'^ {0,3}(`{3,}|~{3,})', line)
        if marker:
            if fence is None:
                fence, start = marker[1], offset
            elif marker[1][0] == fence[0] and len(marker[1]) >= len(fence):
                fenced.append((start, offset + len(line)))
                fence = None
        offset += len(line)
    if fence:
        fenced.append((start, len(text)))
    parts, cursor = [], 0
    for match in pattern.finditer(masked):
        if any(a <= match.start() < b for a, b in fenced):
            continue
        original = pattern.match(text, match.start())
        parts.extend([text[cursor:match.start()], replace(original)])
        cursor = match.end()
    parts.append(text[cursor:])
    return ''.join(parts)


@dataclass(frozen=True)
class TableRow:
    id: str
    cells: tuple[str, ...]
    category: str
    group_heading: bool = False


@dataclass(frozen=True)
class TableVariant:
    id: str
    condition: str
    headers: tuple[str, ...]
    rows: tuple[TableRow, ...]
    context: tuple[str, ...]
    column_roles: tuple[str, ...] = ()

    def __post_init__(self):
        roles = tuple(column_role(h) for h in self.headers)
        if self.column_roles and self.column_roles != roles:
            raise ValueError('Column roles disagree with source headers')
        object.__setattr__(self, 'column_roles', roles)


def _coalesce_duplicate_variants(variants):
    """Collapse identical matrices with known multilevel-header typography.

    Only these layout labels are interchangeable with omitted group labels.
    Dataset/split names, units, directions, conditions and every data cell
    remain part of the identity. Prefer the full header; retain both sources.
    """
    grouped = re.compile(r'^(?:Hyperparams|Hyperparameters|Dev Set Accuracy)\s+', re.I)

    def key(variant):
        indices = [i for i, h in enumerate(variant.headers) if label(h) not in _SOURCE]
        headers = tuple(_column(grouped.sub('', variant.headers[i])) for i in indices)
        values = tuple((row.category, row.group_heading,
                        tuple(_cell_value(row.cells[i], variant.headers[i]) for i in indices))
                       for row in variant.rows)
        return variant.condition, headers, values

    combined = {}
    for variant in variants:
        identity = key(variant)
        previous = combined.get(identity)
        if previous is None:
            combined[identity] = variant
            continue
        base = max((previous, variant), key=lambda v: sum(map(len, v.headers)))
        headers = base.headers
        source_index = next((i for i, h in enumerate(headers) if label(h) in _SOURCE), None)
        has_sources = any(label(h) in _SOURCE for v in (previous, variant) for h in v.headers)
        if source_index is None and has_sources:
            source_index = len(headers)
            headers += ('原文依据',)
        rows = []
        for position, row in enumerate(base.rows):
            cells = list(row.cells)
            if source_index is not None:
                sources = dict.fromkeys(v.rows[position].cells[i] for v in (previous, variant)
                                        for i, h in enumerate(v.headers)
                                        if label(h) in _SOURCE and v.rows[position].cells[i])
                cells += [''] * (len(headers) - len(cells))
                cells[source_index] = ' '.join(sources)
            rows.append(replace(row, cells=tuple(cells)))
        combined[identity] = replace(base, headers=headers, rows=tuple(rows), column_roles=(),
                                     context=tuple(dict.fromkeys(previous.context + variant.context)))
    return reconcile_layouts(tuple(combined.values()))


@dataclass(frozen=True)
class SourceTable:
    id: str
    number: int
    caption: str
    source_page: int | None
    variants: tuple[TableVariant, ...]


@dataclass(frozen=True)
class ReportTables:
    tables: tuple[SourceTable, ...]
    # ID partitions reference immutable raw annotations, never replacement text.
    annotations: tuple[dict, ...] = ()
    source_resolutions: tuple[dict, ...] = ()

    def consolidate_annotations(self, chat):
        if not self.tables or self.annotations:
            return self
        material = [{'id': t.id, 'units': annotation_units(t)} for t in self.tables]
        try:
            response = extract_json_object(chat(ANNOTATION_SYSTEM, ANNOTATION_PROMPT + json.dumps(material, ensure_ascii=False)))
        except (ModelBudgetExceeded, CheckpointWriteError):
            raise
        except Exception:
            # An optional semantic selection must not block report publication.
            return self
        proposed = response.get('tables', []) if isinstance(response, dict) else []
        if not isinstance(proposed, list):
            return self
        retained = []
        for table in self.tables:
            matches = [p for p in proposed if isinstance(p, dict) and p.get('id') == table.id]
            if len(matches) == 1 and validate_groups(annotation_units(table), matches[0].get('groups')):
                retained.append({'id': table.id, 'groups': matches[0]['groups']})
        return replace(self, annotations=tuple(retained))

    @classmethod
    def from_notes(cls, notes, inventory, *, source_pages=()):
        # Old extraction formats are consolidated exactly once, independently
        # of the generated prose. Keep unmatched schemas and conflicting cells.
        notes, resolutions = reconcile_source_matrices(notes, source_pages)
        known = {item['number']: item for item in inventory}
        for note in notes:
            for number, *_ in _blocks(note):
                known.setdefault(number, {'number': number})
        imported = restore_note_tables('', notes, list(known.values()), source_pages=source_pages,
                                       integrate=False)
        return replace(cls._from_markdown(imported, known), source_resolutions=resolutions)

    @classmethod
    def _from_markdown(cls, text, inventory):
        headings = _headings(text)
        collected = {}
        for number, rows, _, context in _blocks(text):
            title = rows[0]['table_title']
            end = next((h[0] for h in headings if h[0] > rows[-1]['end']), len(text))
            paragraphs = re.split(r'\n\s*\n', context + '\n\n' + text[rows[-1]['end']:end])
            paragraphs = _table_context(dict.fromkeys(p.strip() for p in paragraphs
                                                       if p.strip() and p.strip() != title))
            headers = tuple(rows[0]['headers'])
            condition = rows[0].get('heading_qualifier', '')
            variant_id = 'variant-' + _digest([headers, condition])[:20]
            table_rows = []
            for row in rows:
                values = tuple(value.strip() for value, _, _ in row['cells'])
                category = str(row.get('category', ''))
                identity = [category, condition, [(h, _cell_value(v, h)) for h, v in zip(headers, values)
                                                  if label(h) not in _SOURCE]]
                table_rows.append(TableRow('row-' + _digest(identity)[:20], values, category,
                                           bool(row.get('category_heading_row'))))
            variant = TableVariant(variant_id, condition, headers, tuple(table_rows), paragraphs)
            entry = collected.setdefault(number, {'caption': title, 'variants': []})
            entry['variants'].append(variant)
        return cls(tuple(SourceTable(f'table-{number}', number, data['caption'],
                                    inventory.get(number, {}).get('page'), _coalesce_duplicate_variants(data['variants']))
                         for number, data in sorted(collected.items())))

    def to_dict(self):
        payload = {'version': 3, 'tables': [asdict(table) for table in self.tables],
                   'annotations': list(self.annotations),
                   'source_resolutions': list(self.source_resolutions),
                   'assessment': 'extracted_cells; PDF completeness and correctness remain unassessed'}
        return {**payload, 'sha256': _digest(payload)}

    @classmethod
    def from_dict(cls, data):
        payload = {k: v for k, v in data.items() if k != 'sha256'}
        if data.get('version') not in {1, 2, 3} or data.get('sha256') != _digest(payload):
            raise ValueError('Invalid or changed table catalogue')
        tables = []
        for table in data['tables']:
            if table['id'] != f"table-{table['number']}":
                raise ValueError('Invalid table identity')
            variants = []
            for variant in table['variants']:
                rows = tuple(TableRow(row['id'], tuple(row['cells']), row['category'], row['group_heading'])
                             for row in variant['rows'])
                if any(len(row.cells) != len(variant['headers']) for row in rows):
                    raise ValueError('Unbound table cells')
                variants.append(TableVariant(variant['id'], variant['condition'], tuple(variant['headers']),
                                             rows, tuple(variant['context']), tuple(variant.get('column_roles', ()))))
            tables.append(SourceTable(table['id'], table['number'], table['caption'],
                                      table['source_page'], tuple(variants)))
        if len({t.id for t in tables}) != len(tables):
            raise ValueError('Duplicate source table identities')
        annotations = data.get('annotations', [])
        known = {t.id: t for t in tables}
        if not isinstance(annotations, list) or any(
                not isinstance(a, dict) or a.get('id') not in known or
                not validate_groups(annotation_units(known[a['id']]), a.get('groups')) for a in annotations):
            raise ValueError('Invalid table annotation selection')
        if len({a['id'] for a in annotations}) != len(annotations):
            raise ValueError('Duplicate table annotation selections')
        return cls(tuple(tables), tuple(annotations), tuple(data.get('source_resolutions', ())))

    def synthesis_notes(self, notes):
        """Keep note analysis; replace all original-table copies with identities."""
        return [self.prepare_draft(note) for note in apply_source_resolutions(notes, self.source_resolutions)]

    def prose(self, report):
        """Remove owned displays before any prose/outline transformation."""
        return _sub_directives(_MANAGED, lambda m: f'[[表格:{m[1]}|{"折叠" if "<details" in m[0] else "展开"}]]', report)

    def prompt_material(self):
        return json.dumps({'tables': [asdict(table) for table in self.tables]}, ensure_ascii=False)

    def prepare_draft(self, draft):
        known = {table.number for table in self.tables}
        edits = []
        # Replace cells only. Narrative around a generated table remains prose;
        # source data always comes from the frozen extraction, not this copy.
        for number, rows, _, _ in _blocks(draft):
            if number in known:
                edits.append((rows[0]['header_start'], rows[-1]['end'], f'[[表格:table-{number}]]'))
        for start, end, replacement in sorted(edits, reverse=True):
            draft = draft[:start] + replacement + draft[end:]
        # Captions are owned by the renderer. Preserve mixed topic headings.
        for start, end, _, title in reversed(_headings(draft)):
            match = re.match(r'^(?:Table\s+(\d+)\b|表\s*(\d+)\b)', title, re.I)
            if match and int(match[1] or match[2]) in known:
                draft = draft[:start] + draft[end:]
        return draft

    def render(self, draft):
        # Managed blocks round-trip to slots, so retries never append a second
        # copy. The same immutable cells are used on every render.
        draft = self.prose(draft)
        draft = self.prepare_draft(draft)
        draft = _clean_recovered_model_notes(draft, {table.number for table in self.tables})
        by_id = {table.id: table for table in self.tables}
        displayed = set()

        def replace(match):
            table = by_id.get(match[1])
            if table is None:
                # A placement instruction carries no paper data. Its original
                # text remains in report-draft.md, never as reader-facing UI.
                return ''
            if table.id in displayed:
                return f'[Table {table.number}](#paper-{table.id})'
            displayed.add(table.id)
            return self._render_table(table, match[2])

        draft = _sub_directives(_SLOT, replace, draft)
        for table in self.tables:
            if table.id not in displayed:
                draft, position, _ = _destination(draft, table.number, table.caption)
                draft = (draft[:position].rstrip() + '\n\n' + self._render_table(table, None)
                         + '\n\n' + draft[position:])
        return draft

    def _render_table(self, table, display):
        selection = next((a['groups'] for a in self.annotations if a['id'] == table.id), None)
        caption, notes = display_annotations(table, selection)
        lines = [f'<!-- paperloom-table:{table.id}:start -->', f'<a id="paper-{table.id}"></a>', '',
                 '#### ' + caption, '']
        for variant, note in zip(table.variants, notes):
            if len(table.variants) > 1 and variant.condition:
                lines.extend(['**实验条件：' + variant.condition + '**', ''])
            if note:
                lines.extend(['**说明：** ' + note, ''])
            lines.extend(['| ' + ' | '.join(variant.headers) + ' |',
                          '| ' + ' | '.join(['---'] * len(variant.headers)) + ' |'])
            lines.extend('| ' + ' | '.join(row.cells) + ' |' for row in variant.rows)
            lines.append('')
        folded = display == '折叠' or (display is None and sum(len(v.rows) for v in table.variants) >= 15)
        if folded:
            lines.insert(2, f'<details class="report-table-details">\n<summary>完整模型与指标结果（Table {table.number}）</summary>\n')
            lines.extend(['</details>', ''])
        lines.append(f'<!-- paperloom-table:{table.id}:end -->')
        return '\n'.join(lines)

    def review(self, report):
        """Check literal experimental cells and one managed display per identity."""
        issues = []
        displays = {}
        for match in _MANAGED.finditer(report):
            displays.setdefault(match[1], []).append(match[0])
        for table in self.tables:
            blocks = displays.get(table.id, [])
            if len(blocks) != 1:
                issues.append({'table_id': table.id, 'reason': 'display_count', 'count': len(blocks)})
                continue
            captions = re.findall(r'(?im)^\s*(?:#{1,6}\s+)?(?:Table\s+' + str(table.number) + r'\b|表\s*' + str(table.number) + r'\b)[：:.\s]', blocks[0])
            if len(captions) != 1:
                issues.append({'table_id': table.id, 'reason': 'caption_count', 'count': len(captions)})
            selection = next((a['groups'] for a in self.annotations if a['id'] == table.id), None)
            if selection is None:
                selection = [[u['id']] for u in annotation_units(table)]
            if selection:
                # Detect a later presentation stage reintroducing a selected
                # note. Reference decoration may change; scientific prose may not.
                plain = lambda s: re.sub(r'\s+', '', _LINK.sub('', TOKEN.sub('', s))).replace('`', '').replace('*', '')
                def annotations_only(text):
                    # Cells have their own preservation check. In particular,
                    # evidence-cell diagnostics may disappear during publication.
                    # Use parsed matrices, not a line prefix: scientific notes
                    # may themselves start with an absolute-value/math bar.
                    for _, rows, *_ in reversed(list(_blocks(text))):
                        text = text[:rows[0]['header_start']] + text[rows[-1]['end']:]
                    return text
                baseline = plain(annotations_only(self._render_table(table, None)))
                observed_text = plain(annotations_only(blocks[0]))
                units = {u['id']: u for u in annotation_units(table)}
                for group in selection:
                    # Clause punctuation may change when it ends the note;
                    # compare the content, including short unit/condition notes.
                    note = plain(units[group[0]]['text']).rstrip('。.，,；;')
                    if not note:
                        continue
                    expected_count, actual_count = baseline.count(note), observed_text.count(note)
                    if actual_count != expected_count:
                        issues.append({'table_id': table.id, 'reason': 'repeated_annotation' if actual_count > expected_count
                                       else 'missing_or_changed_annotation', 'unit': group[0]})
            actual = list(_blocks(blocks[0]))
            expected = [[tuple((h, _cell_value(v, h)) for h, v, role in zip(variant.headers, row.cells, variant.column_roles)
                              if role == 'data') for row in variant.rows]
                        for variant in table.variants]
            observed = [[tuple((h, _cell_value(c[0].replace('（引用冲突）', ''), h)) for h, c in zip(row['headers'], row['cells'])
                              if label(h) not in _SOURCE) for row in rows] for _, rows, *_ in actual]
            if expected != observed:
                issues.append({'table_id': table.id, 'reason': 'changed_cells_or_order'})
        expected_blocks = {table.number: len(table.variants) for table in self.tables}
        observed_blocks = {}
        for number, *_ in _blocks(report):
            if number in expected_blocks:
                observed_blocks[number] = observed_blocks.get(number, 0) + 1
        if expected_blocks != observed_blocks:
            issues.append({'reason': 'unmanaged_or_missing_table_blocks'})
        return {'status': 'passed' if not issues else 'failed', 'issues': issues,
                'tables': len(self.tables), 'rows': sum(len(v.rows) for t in self.tables for v in t.variants)}


class GeneratedReport(str):
    """A report and its table provenance, carried together without shared state."""
    def __new__(cls, text, tables, draft):
        result = super().__new__(cls, text)
        result.tables = tables
        result.draft = draft
        return result
