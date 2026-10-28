"""Legacy Markdown input adapter for table structure, independent of report placement."""
from __future__ import annotations

import re

from ..evidence import TOKEN
from .table_quality import cells, label, mask_quotes, normalize_table_separators, tables


SOURCE_LINK = re.compile(r'\[\d+\]\(paper\.pdf#page=\d+(?: "[^"]*")?\)')
SOURCE_HEADERS = {'依据', '原文依据', '支持依据'}
CATEGORY_HEADERS = {'模型分类', 'model category', 'level', 'difficulty', '难度'}
_CONDITION = re.compile(
    r'\b(?:training|test|dev|validation)\s+(?:split|set|data(?:set)?)\b|'
    r'\b(?:temperature|seed|batch(?:\s+size)?|precision|conditions?|settings?|dataset|units?)\s*[:=：]|'
    r'\b(?:fp|bf)\d+\b|(?:训练集|测试集|验证集|数据集|实验条件|温度|种子|批量|精度|单位)\s*[:=：]', re.I)


def column_key(text):
    value = label(text)
    value = re.sub(r'\\\(\s*\\(uparrow|downarrow)\s*\\\)|\$\s*\\(uparrow|downarrow)\s*\$',
                   lambda m: '↑' if (m[1] or m[2]) == 'uparrow' else '↓', value)
    # Closed presentation aliases only: conditions, units and directions stay
    # in the key. Never use numeric similarity to infer a column mapping.
    aliases = {'方法 (method)': 'method', '模型 (model)': 'model', 'models': 'model', 'methods': 'method',
               'level': 'difficulty', '难度': 'difficulty', '模型分类': 'model category',
               '统计指标 (statistic)': 'statistic', '对应数值 (value)': 'value',
               '准则生成器 (rubric generator)': 'rubric generator',
               '胜 (win)': 'win', '平 (tie)': 'tie', '负 (loss)': 'loss',
               '胜率 (win rate)': 'win rate', '胜率': 'win rate',
               '对比项 (comparison)': 'comparison', '对比项 (baseline)': 'comparison',
               'baseline': 'comparison', 'rl 奖励设计': 'method', '模型': 'model', '方法': 'method',
               '类别 / 模型': 'model', '模型 / 方案': 'model', '方法/模型': 'model',
               '总计 (overall)': 'overall', 'overall (总计)': 'overall'}
    value = aliases.get(value, value)
    value = re.sub(r'\bmmsvg-(?=illustration\b|icon\b)', '', value)
    return re.sub(r'[\s:：]+', '', value)


def cell_key(value, header):
    value = text_key(value)
    if value in {'', '-', '–', '—'}:
        return '-'
    if column_key(header) in {'method', 'model'}:
        value = re.sub(r'\s*[（(]本文[）)]$', '', value)
        # Typography around annotations is not a different model variant.
        # Preserve spaces within names: "GPT-4o mini" != "GPT-4omini".
        value = re.sub(r'\s*([()\[\]])\s*', r'\1', value)
    if column_key(header) == 'comparison':
        value = re.sub(r'^vs\.?\s+', '', value, flags=re.I)
    if column_key(header) == 'rubricgenerator' and value in {'-', '–', '—'}:
        value = '-'
    if column_key(header) == 'statistic':
        aliases = {'提示词数 (Prompts)': 'Prompts', '平均输入 Token (Avg. Input Tokens)': 'Avg. Input Tokens',
                   '平均准则 Token (Avg. Rubric Tokens)': 'Avg. Rubric Tokens',
                   '平均输出 Token (Avg. Output Tokens)': 'Avg. Output Tokens',
                   '总成本 (Total Cost)': 'Total Cost', '单提示词成本 (Cost / Prompt)': 'Cost / Prompt',
                   'Prompts 数量': 'Prompts', '平均输入 Tokens (Avg. Input Tokens)': 'Avg. Input Tokens',
                   '平均细则 Tokens (Avg. Rubric Tokens)': 'Avg. Rubric Tokens',
                   '平均输出 Tokens (Avg. Output Tokens)': 'Avg. Output Tokens',
                   '总开销 (Total Cost, 美元)': 'Total Cost', '单条 Prompt 成本 (Cost / Prompt)': 'Cost / Prompt'}
        value = aliases.get(value, value)
    return re.sub(r'\s*±\s*', '±', value)


def text_key(text):
    # Presentation and evidence are not experimental data. Do not normalise
    # numbers, units, model names or conditions when comparing rows.
    text = TOKEN.sub('', text)
    text = SOURCE_LINK.sub('', text)
    text = re.sub(r'\*\*([^*]+)\*\*|`([^`]+)`', lambda m: m[1] or m[2], text)
    return re.sub(r'\s+', ' ', text).strip()


def table_blocks(text):
    headings = section_headings(text)
    groups = {}
    for row in tables(text, TOKEN):
        groups.setdefault(row['header_start'], []).append(row)
    for start, rows in groups.items():
        heading = next((h for h in reversed(headings) if h[0] < start), None)
        number = re.search(r'\bTable\s+(\d+)\b|表\s*(\d+)\b', heading[3], re.I) if heading else None
        if not number:
            continue
        context = text[heading[1]:start]
        title = re.sub(r'[（(](?:部分摘录|部分数据重现|具备文本引用之部分)[）)]', '', heading[3]).strip()
        title = re.sub(r'(?<=\d)\s+部分数据重现', '', title)
        title = re.sub(r'^原文标题\s*[：:]\s*', '', title)
        # Bilingual caption translations commonly appear in parentheses. They
        # are context to preserve, not a different experimental setting. Bind
        # explicit setting qualifiers; preserve all other title text below too.
        setting = re.compile(r'=|\b(?:temperature|seed|batch|fp\d+|bf\d+|ablation|split|precision)\b|温度|种子|批量|消融|划分', re.I)
        qualifiers = [value.strip() for value in re.findall(r'[（(]([^）)]+)[）)]', title)
                      if setting.search(value)]
        if '=' in title and not qualifiers:
            qualifiers.append(re.sub(r'\bTable\s+\d+\b|表\s*\d+', '',
                                     TOKEN.sub('', title), flags=re.I).strip(' .:：。'))
        # Keep the introductory conditions; don't duplicate earlier sibling tables.
        earlier = [position for position in groups if heading[1] < position < start]
        if earlier:
            local = text[groups[max(earlier)][-1]['end']:start]
            shared = text[heading[1]:min(earlier)]
            # An explicit introduction between sibling matrices belongs to
            # the following one. Otherwise inherit the common introduction.
            context = local if _CONDITION.search(text_key(local)) else shared + '\n\n' + local
        # Conditions can be separate notes before or after a matrix, rather
        # than caption suffixes. Bind them for every consumer of this adapter:
        # source reconciliation, row deduplication and frozen variants.
        stop = min([len(text), *(h[0] for h in headings if h[0] > rows[-1]['end'])])
        # Do not also claim a following matrix's introduction as this one's
        # trailing footnote. Only the last matrix owns the section tail.
        trailing = '' if any(start < position < stop for position in groups) else text[rows[-1]['end']:stop]
        condition_context = []
        for paragraph in re.split(r'\n\s*\n', context + '\n\n' + trailing):
            value = text_key(paragraph).strip(' *')
            if _CONDITION.search(value):
                qualifiers.append(value)
                condition_context.append(paragraph.strip())
        qualifier = ' '.join(dict.fromkeys(qualifiers))
        if re.sub(r'\bTable\s+\d+\b|表\s*\d+', '', title, flags=re.I).strip():
            context = title + '\n\n' + context
        # Task tables commonly use a merged first column. Make its meaning
        # explicit before deduplication; identical scores in different tasks
        # must never collapse into one row.
        task = ''
        category = ''
        category_heading = None
        category_columns = [i for i, header in enumerate(rows[0]['headers']) if label(header) in CATEGORY_HEADERS]
        for row in rows:
            row['table_title'] = title
            row['heading_qualifier'] = qualifier
            row['condition_context'] = tuple(condition_context)
            row['cells'] = [(row['raw'][begin:end], begin, end) for _, begin, end in row['cells']]
            first = text_key(row['cells'][0][0])
            if category_columns:
                category_values = sorted((column_key(row['headers'][i]), re.sub(r'\s+models$', '', text_key(row['cells'][i][0]).casefold()))
                                         for i in category_columns)
                row['category'] = category_values[0][1] if len(category_values) == 1 else tuple(category_values)
            elif first and all(not text_key(cell[0]) for header, cell in zip(row['headers'][1:], row['cells'][1:])
                               if label(header) not in SOURCE_HEADERS):
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
                    task = value.strip()
                elif task:
                    row['cells'][0] = (task, begin, finish)
                    row['raw'] = row['raw'][:begin] + ' ' + task + ' ' + row['raw'][finish:]
                    # Expanding a merged task cell shifts every later column.
                    # Recompute local offsets before rendering or merging it.
                    row['cells'] = [(row['raw'][a:b], a, b) for _, a, b in cells(mask_quotes(row['raw'], TOKEN))]
                    row['cells'] += [('', len(row['raw']), len(row['raw']))] * (len(row['headers']) - len(row['cells']))
        yield int(number[1] or number[2]), rows, text[start:rows[-1]['end']], context


def section_headings(text):
    """Ignore fenced examples and quoted evidence while finding real sections."""
    fence = None
    offset = 0
    result = []
    for line in mask_quotes(text, TOKEN).splitlines(keepends=True):
        marker = re.match(r'^ {0,3}(`{3,}|~{3,})', line)
        if marker:
            if fence is None:
                fence = marker[1]
            elif marker[1][0] == fence[0] and len(marker[1]) >= len(fence):
                fence = None
        elif not fence and (match := re.match(r'^(#{2,6})\s+([^\n]+)', line)):
            # Masking is only for structural discovery. Display text must come
            # from the original offsets, including any source reference.
            title = text[offset + match.start(2):offset + match.end(2)].strip()
            result.append((offset, offset + len(line), len(match[1]), title))
        offset += len(line)
    return result


def source_table_headers(pages):
    result = {}
    for page in pages:
        captions = list(re.finditer(r'(?im)^\s*Table\s+(\d+)\s*[:.]', page))
        for index, caption in enumerate(captions):
            end = captions[index + 1].start() if index + 1 < len(captions) else len(page)
            lines = page[caption.end():end].splitlines()[1:]
            header_lines = []
            for line in lines:
                # Physical PDF extraction puts the header above the first scalar.
                # Stop at data, so row labels/prose cannot complete a column name.
                if re.fullmatch(r'[+−-]?\d[\d,.]*(?:\s*%)?', line.strip()):
                    break
                if line.strip():
                    header_lines.append(line.strip())
            result.setdefault(int(caption[1]), []).append(header_lines)
    return result


def restore_source_header_names(text, source_headers):
    """Restore a shortened name only when this source table gives one completion.

    Never equate model variants globally, or use their scores to infer identity.
    """
    for number, rows, _, _ in reversed(list(table_blocks(text))):
        headers = rows[0]['headers'][:]
        if [column_key(h) for h in headers[:2]] != ['task', 'generator']:
            continue
        owner = label(text_key(rows[0]['cells'][0][0]))
        completions = set()
        for lines in source_headers.get(number, []):
            start = next((i for i, line in enumerate(lines) if column_key(line) == 'task'), None)
            end = next((i for i, line in enumerate(lines) if start is not None and i > start and label(line) == owner), None)
            if start is None or end is None:
                continue
            original = [line for line in lines[start:end]
                        if label(line) not in {'models used as judges', 'vlm judges', 'judges'}]
            indices = [i for i, h in enumerate(headers) if label(h) not in SOURCE_HEADERS | CATEGORY_HEADERS]
            if len(original) != len(indices):
                continue
            restored = headers[:]
            valid = True
            for index, full in zip(indices, original):
                header = headers[index]
                if column_key(header) == column_key(full):
                    continue
                name = label(header)
                suffix = label(full).removeprefix(name + ' ')
                # A complete positional schema anchors a shortened model name.
                # Numerical versions, units and directions are never completed.
                if (not re.search(r'\d', name) or not label(full).startswith(name + ' ')
                        or not re.fullmatch(r'[a-z][a-z .-]*', suffix)
                        or sum(label(line).startswith(name + ' ') for line in original) != 1):
                    valid = False
                    break
                restored[index] = full
            if valid:
                completions.add(tuple(restored))
        if len(completions) == 1 and tuple(headers) not in completions:
            headers = list(next(iter(completions)))
            end = text.find('\n', rows[0]['header_start'])
            end = len(text) if end < 0 else end
            text = text[:rows[0]['header_start']] + '| ' + ' | '.join(headers) + ' |' + text[end:]
    return text


def normalize_model_groups(text):
    """Expand a closed Open/Closed merged column, never an arbitrary blank one."""
    for _, rows, _, _ in reversed(list(table_blocks(text))):
        headers = rows[0]['headers']
        if len(headers) < 2 or headers[0].strip() or column_key(headers[1]) != 'model':
            continue
        groups = {text_key(row['cells'][0][0]) for row in rows if text_key(row['cells'][0][0])}
        if not groups or not groups <= {'Open', 'Closed'}:
            continue
        lines = ['| ' + ' | '.join(['Model Category', *headers[1:]]) + ' |',
                 '| ' + ' | '.join(['---'] * len(headers)) + ' |']
        group = ''
        for row in rows:
            values = [row['raw'][a:b].strip() for _, a, b in row['cells']]
            if values[0]:
                group = values[0]
            values[0] = group
            lines.append('| ' + ' | '.join(values) + ' |')
        text = text[:rows[0]['header_start']] + '\n'.join(lines) + text[rows[-1]['end']:]
    return text


def _before_next_condition(text):
    paragraphs = re.finditer(r'(?m)^.+(?:\n(?!\n).+)*', text)
    stop = next((m.start() for m in paragraphs if _CONDITION.search(text_key(m[0]))), len(text))
    return text[:stop]


def note_extractions(notes, *, source_pages=()):
    """Read each original matrix once, retaining its own conditions and footnotes."""
    source_headers = source_table_headers(source_pages)
    for note in notes:
        text = normalize_table_separators(note, TOKEN)
        text = restore_source_header_names(normalize_model_groups(text), source_headers)
        blocks = list(table_blocks(text))
        sections = section_headings(text)
        for index, (number, rows, _, context) in enumerate(blocks):
            start, end = rows[0]['header_start'], rows[-1]['end']
            stop = next((h[0] for h in sections if h[0] > end), len(text))
            following = next((b for b in blocks[index + 1:] if b[1][0]['header_start'] < stop), None)
            if following:
                between = text[end:following[1][0]['header_start']]
                trailing = _before_next_condition(between)
            else:
                trailing = text[end:stop]
            if index and blocks[index - 1][1][-1]['end'] < start:
                previous_end = blocks[index - 1][1][-1]['end']
                same_section = not any(previous_end < h[0] < start for h in sections)
                if same_section:
                    between = text[previous_end:start]
                    previous_tail = _before_next_condition(between).strip()
                    if previous_tail:
                        context = context.replace(previous_tail, '', 1)
            paragraphs = tuple(dict.fromkeys(p.strip() for p in
                               re.split(r'\n\s*\n', context + '\n\n' + trailing)
                               if p.strip() and p.strip() != rows[0]['table_title']))
            yield number, rows, paragraphs
