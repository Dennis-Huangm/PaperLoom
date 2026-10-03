"""One Markdown dialect and structural checks for all generated reports."""
from __future__ import annotations

from collections import Counter
import html
import re
import unicodedata
from xml.etree import ElementTree as ET

from bs4 import BeautifulSoup
from markdown.extensions.toc import slugify, unique
from markdown_it import MarkdownIt
from markdown_it.rules_block.table import escapedSplit, table as parse_table
from markdown_it.rules_inline.emphasis import tokenize as parse_emphasis


class ReportRenderError(ValueError):
    """Do not publish an HTML report that lost recognized Markdown structure."""


def _cjk_letter(char):
    return unicodedata.category(char).startswith('L') and unicodedata.name(char, '').startswith(
        ('CJK ', 'HIRAGANA ', 'KATAKANA ', 'HANGUL '))


def _emphasis(state, silent):
    """Allow punctuation-ended bold next to unspaced CJK prose.

    CommonMark rejects `**有用性（Helpfulness）**仅评估` because the
    closing punctuation is followed by a letter. Extend only that boundary,
    keeping the normal delimiter pairing, nesting and protected token rules.
    Source Markdown and report data are never rewritten to insert spaces.
    """
    start, first = state.pos, len(state.delimiters)
    accepted = parse_emphasis(state, silent)
    if not accepted or state.src[start] != '*' or state.pos - start < 2:
        return accepted
    before = state.src[start - 1] if start else ' '
    after = state.src[state.pos] if state.pos < state.posMax else ' '
    can_open = _cjk_letter(before) and unicodedata.category(after).startswith('P')
    can_close = unicodedata.category(before).startswith('P') and _cjk_letter(after)
    for delimiter in state.delimiters[first:]:
        delimiter.open |= can_open
        delimiter.close |= can_close
    return accepted


def _math_inline(state, silent):
    start = state.pos
    if state.src.startswith(r'\(', start):
        opening, closing = r'\(', r'\)'
    elif state.src[start] == '$' and not state.src.startswith('$$', start):
        opening = closing = '$'
        if start + 1 >= state.posMax or state.src[start + 1].isspace():
            return False
    else:
        return False
    end = start + len(opening)
    while True:
        end = state.src.find(closing, end, state.posMax)
        if end < 0:
            return False
        if (len(state.src[:end]) - len(state.src[:end].rstrip('\\'))) & 1:
            end += len(closing)
            continue
        # Dollar amounts in adjacent cells are not a paired TeX expression.
        # Apply the same delimiter boundaries used by table splitting below.
        if opening == '$' and (state.src[end - 1].isspace()
                               or (end + 1 < state.posMax and state.src[end + 1].isdigit())):
            return False
        break
    content = state.src[start + len(opening):end]
    if not content.strip() or '\n' in content:
        return False
    if not silent:
        token = state.push('math_inline', 'span', 0)
        token.content = content.strip()
    state.pos = end + len(closing)
    return True


def _math_block(state, start, end, silent):
    if state.is_code_block(start):
        return False
    first = state.src[state.bMarks[start] + state.tShift[start]:state.eMarks[start]]
    if first.startswith(r'\['):
        opening, closing = r'\[', r'\]'
    elif first.startswith('$$'):
        opening = closing = '$$'
    else:
        match = re.match(r'\\begin\{(equation\*?|align\*?|aligned|gather\*?|multline\*?)\}', first)
        if not match:
            return False
        opening, closing = match[0], r'\end{' + match[1] + '}'
    parts = []
    for line in range(start, end):
        if line > start and state.sCount[line] < state.blkIndent and not state.isEmpty(line):
            return False
        value = state.src[state.bMarks[line] + state.tShift[line]:state.eMarks[line]]
        if line == start:
            value = value[len(opening):]
        if closing in value:
            content, tail = value.split(closing, 1)
            if tail.strip() and _FORMULA_CITATION.sub('', tail).strip():
                return False
            parts.append(content)
            if silent:
                return True
            token = state.push('math_block', 'div', 0)
            token.content = '\n'.join(parts).strip()
            token.map = [start, line + 1]
            if tail.strip():
                state.push('paragraph_open', 'p', 1)
                citation = state.push('inline', '', 0)
                citation.content = tail.strip()
                citation.children = []
                state.push('paragraph_close', 'p', -1)
            state.line = line + 1
            return True
        parts.append(value)
    return False


_FORMULA_CITATION = re.compile(
    r'\[((?:原文\s+\d+(?:\s*·\s*PDF\s*第\s*\d+\s*页)?)|\d+)\]\((paper\.pdf#page=\d+)'
    r'(?:\s+"原文第\s+\d+\s+页")?\)')


def _formula_citations(content):
    """Move resolved PDF citations out of TeX without interpreting arbitrary links.

    Models sometimes put evidence tokens inside \\text{...}; evidence resolution
    then introduces Markdown and a # fragment that KaTeX cannot parse. Repair at
    the shared math-token boundary so fresh reports and saved reports both work,
    while code spans, mathematical brackets and ordinary \\text stay untouched.
    """
    links = []

    def take(match):
        links.append(f'<a href="{html.escape(match[2], quote=True)}">{html.escape(match[1])}</a>')
        return ''

    repaired = _FORMULA_CITATION.sub(take, content)
    if links:
        # Remove only text wrappers emptied by citation extraction, together
        # with their optional spacing command. Keep mixed text such as units.
        repaired = re.sub(r'(?:\\(?:quad|qquad)\s*)?\\(?:text|textrm|textnormal)\{\s*\}', '', repaired)
        repaired = re.sub(r'\s*\\(?:quad|qquad)\s*$', '', repaired).strip()
    return repaired, ' '.join(links)


def _render_math(renderer, tokens, index, options, env):
    token = tokens[index]
    block = token.type == 'math_block'
    tag, kind, display = ('div', 'block', 'true') if block else ('span', 'inline', 'false')
    content, citations = _formula_citations(token.content)
    rendered = f'<{tag} class="math-{kind}" data-display="{display}">{html.escape(content)}</{tag}>'
    return rendered + (' ' + citations if citations else '')


# Existing reports allow a literal pipe in inline code inside table cells.
# Mask only balanced code spans for table splitting; the inline parser still
# receives the original code. Equal-length replacement preserves source maps.
_CODE_SPAN = re.compile(r'(?<!`)(`+)(?!`)(.+?)(?<!`)\1(?!`)', re.S)
_TABLE_MATH = re.compile(r'\\\([^\n]+?\\\)|(?<!\\)\$(?![\s$])[^\n$]+?(?<![\s\\])\$(?!\d)')


def _table(state, start, end, silent):
    original = state.src
    if start + 1 >= end or '|' not in original[state.bMarks[start]:state.eMarks[start]]:
        return False
    separator = original[state.bMarks[start + 1] + state.tShift[start + 1]:state.eMarks[start + 1]]
    if not re.fullmatch(r'[ |:\-\t]+', separator):
        return False
    marker = next(chr(n) for n in range(0xE000, 0xF8FF) if chr(n) not in original)
    begin = state.bMarks[start]
    finish = state.eMarks[min(start + 1, end - 1)]
    # Limit candidate body to pipe rows, so ordinary prose immediately after a
    # table cannot silently become a padded data row.
    stop = min(start + 2, end)
    while stop < end:
        row = original[state.bMarks[stop]:state.eMarks[stop]]
        if '|' not in row or not row.strip():
            break
        finish = state.eMarks[stop]
        stop += 1
    fragment = original[begin:finish]
    fragment = '\n'.join(_CODE_SPAN.sub(lambda m: m[0].replace('|', marker), line)
                         for line in fragment.split('\n'))
    fragment = _TABLE_MATH.sub(lambda m: m[0].replace('|', marker), fragment)
    count = len(state.tokens)
    try:
        state.src = original[:begin] + fragment + original[finish:]
        accepted = parse_table(state, start, stop, silent)
        if accepted and not silent:
            width = sum(t.type == 'th_open' for t in state.tokens[count:])
            for token in state.tokens[count:]:
                if token.type != 'tr_open' or not token.map:
                    continue
                line = token.map[0]
                cells = escapedSplit(state.src[state.bMarks[line] + state.tShift[line]:state.eMarks[line]].strip())
                if cells and cells[0] == '':
                    cells.pop(0)
                if cells and cells[-1] == '':
                    cells.pop()
                if len(cells) > width:
                    raise ReportRenderError(f'报告 Markdown 第 {line + 1} 行表格列数超过表头，已停止发布 HTML，避免丢失单元格。')
    finally:
        state.src = original
    if accepted and not silent:
        headers = []
        for token in state.tokens[count:]:
            token.content = token.content.replace(marker, '|')
            if token.type == 'inline' and len(headers) < width:
                headers.append(token.content.strip())
            if token.tag in {'th', 'td'} and token.nesting == 1:
                alignment = token.attrs.pop('style', '').removeprefix('text-align:')
                if alignment:
                    token.attrSet('align', alignment)
        # Comparison matrices contain paragraphs, unlike compact numeric tables.
        if 3 <= width <= 6 and headers == ['维度'] + [f'P{i}' for i in range(1, width)]:
            state.tokens[count].attrSet('class', 'comparison-matrix')
    return accepted


def _parser():
    parser = MarkdownIt('commonmark', {'html': True}).enable(['table', 'strikethrough'])
    parser.inline.ruler.at('emphasis', _emphasis)
    parser.inline.ruler.before('escape', 'report_math_inline', _math_inline)
    parser.block.ruler.before('fence', 'report_math_block', _math_block,
                              {'alt': ['paragraph', 'reference', 'blockquote', 'list']})
    parser.block.ruler.at('table', _table, {'alt': ['paragraph', 'reference']})
    parser.add_render_rule('math_inline', _render_math)
    parser.add_render_rule('math_block', _render_math)
    return parser


def _plain(children):
    return ''.join(_plain(t.children) if t.children else t.content
                   for t in children or [] if t.type not in {'html_inline'})


def _headings(tokens):
    """Keep existing Python-Markdown anchor naming for saved report links."""
    used = set()
    root = ET.Element('div', {'class': 'toc'})
    top = ET.SubElement(root, 'ul')
    current_h2 = None
    nested = None
    for i, token in enumerate(tokens):
        if token.type != 'heading_open':
            continue
        label = _plain(tokens[i + 1].children)
        anchor = unique(slugify(label, '-'), used)
        token.attrSet('id', anchor)
        if token.tag not in {'h2', 'h3'}:
            continue
        if token.tag == 'h2':
            current_h2 = ET.SubElement(top, 'li')
            nested = None
            entry = current_h2
        else:
            if current_h2 is not None:
                if nested is None:
                    nested = ET.SubElement(current_h2, 'ul')
                entry = ET.SubElement(nested, 'li')
            else:
                entry = ET.SubElement(top, 'li')
        link = ET.SubElement(entry, 'a', {'href': '#' + anchor})
        # Preserve math tokens in labels while retaining the historical anchor.
        # Do not copy heading HTML or nested links into the navigation.
        def append_label(children):
            for child in children or []:
                if child.type == 'html_inline':
                    continue
                if child.type == 'math_inline':
                    ET.SubElement(link, 'span', {'class': 'math-inline', 'data-display': 'false'}).text = child.content
                elif child.children:
                    append_label(child.children)
                elif len(link):
                    link[-1].tail = (link[-1].tail or '') + child.content
                else:
                    link.text = (link.text or '') + child.content
        append_label(tokens[i + 1].children)
    return ET.tostring(root, encoding='unicode', method='html')


def _walk(tokens):
    for token in tokens:
        yield token
        yield from _walk(token.children or [])


def _check_unparsed_blocks(tokens):
    for i, token in enumerate(tokens):
        if token.type != 'inline' or i == 0 or tokens[i - 1].type != 'paragraph_open':
            continue
        text = _CODE_SPAN.sub('', token.content)
        if re.search(r'(?m)^ {0,3}[-+*] +\S|^ *\|?(?: *:?-{3,}:? *\|){2,}', text):
            line = token.map[0] + 1 if token.map else '?'
            raise ReportRenderError(f'报告 Markdown 第 {line} 行附近的列表或表格未正确解析，已停止发布 HTML；原始 Markdown 已保留。')


def render_markdown(source, clean):
    """Parse, sanitize, then validate before any caller publishes the HTML."""
    parser = _parser()
    tokens = parser.parse(source)
    _check_unparsed_blocks(tokens)
    toc = _headings(tokens)
    body = clean(parser.renderer.render(tokens, parser.options, {}))
    expected = Counter(t.tag for t in _walk(tokens)
                       if t.nesting == 1 and t.tag in {'ul', 'ol', 'li', 'table', 'tr', 'th', 'td', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6'})
    math = Counter(t.type for t in _walk(tokens) if t.type in {'math_block', 'math_inline'})
    tree = BeautifulSoup(body, 'html.parser')
    # Raw HTML may add safe elements. It must never remove parsed structures.
    for tag, count in expected.items():
        if len(tree.find_all(tag)) < count:
            raise ReportRenderError(f'报告渲染检查失败：{tag} 结构丢失，已停止发布 HTML。')
    for kind, count in math.items():
        if len(tree.select('.' + kind.replace('_', '-'))) < count:
            raise ReportRenderError('报告渲染检查失败：公式结构丢失，已停止发布 HTML。')
    for token in tokens:
        if token.type == 'heading_open' and tree.find(id=token.attrGet('id')) is None:
            raise ReportRenderError('报告渲染检查失败：目录目标丢失，已停止发布 HTML。')
    return body, clean(toc)
