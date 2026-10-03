"""Reconcile explicit train/test header axes with their long-form extraction.

This adapter never infers a mapping from score similarity. Every dataset,
method, metric and value must be accounted for before a second layout can be
replaced by the original wide matrix. Unrecognized layouts remain available.
"""
from dataclasses import replace
import re
import unicodedata

from .report_completeness import _column, _cell_value, _value


def _name(value):
    text = unicodedata.normalize('NFKC', _value(value)).casefold()
    return ' '.join(re.sub(r'[(),]', ' ', text).split())


def _equivalent_rows(wide, long):
    wi = [i for i, role in enumerate(wide.column_roles) if role == 'data']
    li = [i for i, role in enumerate(long.column_roles) if role == 'data']
    if (wide.condition != long.condition or len(wi) < 3 or len(wide.rows) < 3
            or any(row.category or row.group_heading for v in (wide, long) for row in v.rows)
            or _column(wide.headers[wi[0]]) != 'trainingdata'
            or _column(wide.rows[0].cells[wi[0]]) != 'testdata'
            or len(set(wide.headers[i] for i in wi[1:])) == len(wi) - 1):
        return None
    if [_column(long.headers[i]) for i in li[:2]] != ['trainingdata', 'testdata']:
        return None
    metric = wide.rows[1].cells[wi[0]]
    axes = [(_name(wide.headers[i]), _name(wide.rows[0].cells[i]),
             _column(metric + ' ' + wide.rows[1].cells[i])) for i in wi[1:]]
    if len(set(axes)) != len(axes):
        return None
    metrics = [_column(long.headers[i]) for i in li[2:]]
    if len(set(metrics)) != len(metrics) or set(metrics) != {a[2] for a in axes}:
        return None
    groups = set((a, b) for a, b, _ in axes)
    methods = {_name(row.cells[wi[0]]): index for index, row in enumerate(wide.rows[2:], 2)}
    if len(methods) != len(wide.rows) - 2:
        return None
    expected, required = {}, set()
    for method, index in methods.items():
        for i, axis in zip(wi[1:], axes):
            key = (method, *axis)
            expected[key] = _cell_value(wide.rows[index].cells[i], wide.headers[i])
            if expected[key] != '-':
                required.add(key)
    covered, row_mapping, active_group = set(), [], None
    for row in long.rows:
        first, second = (_name(row.cells[i]) for i in li[:2])
        values = [_cell_value(row.cells[i], long.headers[i]) for i in li[2:]]
        if (first, second) in groups and all(v == '-' for v in values):
            active_group = first, second
            row_mapping.append((0, row))  # sources for the explicit dataset axes
            continue
        candidates = []
        for train, test in groups:
            for method in methods:
                # Explicit long-form axes, or an explicit group row followed
                # by methods. Parentheses/commas only wrap the method label.
                inline = first == train and second == test + ' ' + method
                grouped = active_group == (train, test) and first == method and second == test
                if inline or grouped:
                    candidates.append((method, train, test))
        if len(candidates) != 1:
            return None
        identity = candidates[0]
        for m, value in zip(metrics, values):
            key = (*identity, m)
            if key not in expected or expected[key] != value:
                return None
            if value != '-':
                covered.add(key)
        row_mapping.append((methods[identity[0]], row))
    return row_mapping if covered == required else None


def reconcile_layouts(variants):
    retained = list(variants)
    for index in range(len(retained)):
        if retained[index] is None:
            continue
        for other in range(len(retained)):
            if index == other or retained[other] is None:
                continue
            wide, long = retained[index], retained[other]
            mapping = _equivalent_rows(wide, long)
            if mapping is None:
                continue
            headers = wide.headers
            ref = next((i for i, role in enumerate(wide.column_roles) if role == 'reference'), None)
            long_refs = [i for i, role in enumerate(long.column_roles) if role == 'reference']
            if long_refs and ref is None:
                ref = len(headers)
                headers += ('原文依据',)
            rows = [replace(r, cells=r.cells + ('',) * (len(headers) - len(r.cells))) for r in wide.rows]
            if ref is not None:
                for target, source in mapping:
                    cells = list(rows[target].cells)
                    cells[ref] = ' '.join(dict.fromkeys([cells[ref], *(source.cells[i] for i in long_refs)])).strip()
                    rows[target] = replace(rows[target], cells=tuple(cells))
            retained[index] = replace(wide, headers=headers, column_roles=(), rows=tuple(rows),
                                      context=tuple(dict.fromkeys(wide.context + long.context)))
            retained[other] = None
    return tuple(v for v in retained if v is not None)
