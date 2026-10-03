from pathlib import Path
import pytest
from bs4 import BeautifulSoup

from arxiv_ra.render import markdown_with_math, render_report


@pytest.mark.parametrize('prefix,indent', [('说明文字：', ''), ('### 结果', ''),
                                           ('- **胜率**：', '  '), ('1. **胜率**：', '   '),
                                           ('- **胜率**：', '    ')])
def test_tables_interrupt_prose_and_render_inside_list_items(prefix, indent):
    source = (prefix + '\n' + indent + '| 模型 | 得分 | 依据 |\n'
              + indent + '| :--- | ---: | :---: |\n'
              + indent + '| GPT-4o | 82.72 | [原文](paper.pdf#page=10) |\n'
              + indent + '| DeepSeek-R1 | 74.19 | 待核对 |\n'
              + '\n后续说明。')
    tree = BeautifulSoup(markdown_with_math(source)[0], 'html.parser')
    table = tree.find('table')
    assert table is not None
    assert [[c.get_text() for c in row.select('td')] for row in table.select('tbody tr')] == [
        ['GPT-4o', '82.72', '原文'], ['DeepSeek-R1', '74.19', '待核对']]
    assert table.select_one('a')['href'] == 'paper.pdf#page=10'
    assert table.select('th')[1].get('align') == 'right'
    assert '后续说明' not in table.get_text()
    if indent:
        assert table.find_parent('li') is not None


def test_table_boundaries_preserve_code_examples_and_non_table_pipes():
    source = '''```markdown
说明：
| A | B |
| --- | --- |
| 1 | 2 |
```

    | A | B |
    | --- | --- |
    | 1 | 2 |

ordinary | prose
still | prose
'''
    tree = BeautifulSoup(markdown_with_math(source)[0], 'html.parser')
    assert not tree.find('table')
    assert len(tree.select('pre code')) == 2
    assert '| A | B |' in tree.select_one('pre code').get_text()


def test_table_like_content_in_nested_code_is_not_promoted():
    for source in ['- Example:\n\n        | A | B |\n        | --- | --- |\n        | 1 | 2 |',
                   '- Example:\n  ```markdown\n  | A | B |\n  | --- | --- |\n  | 1 | 2 |\n  ```']:
        assert '<table>' not in markdown_with_math(source)[0]


def test_table_in_list_after_prose_without_any_blank_line():
    source = '''生成任务结果如下：
- **文本到图形**：结论。
- **胜率表**：
  | 模型 | 胜率 |
  | --- | --- |
  | A | 61.54 |
  | B | 50.55 |
'''
    tree = BeautifulSoup(markdown_with_math(source)[0], 'html.parser')
    assert tree.table and tree.table.find_parent('li')
    assert len(tree.select('ul > li')) == 2
    assert len(tree.select('tbody tr')) == 2


def test_table_keeps_math_code_pipes_escaped_pipes_and_following_text():
    source = r'''结果：
| 表达式 | 名称 | 值 |
| --- | --- | --- |
| \(a \mid b\) | `a|b` 与 A\|B | **82.72** |
后续普通文字。
'''
    tree = BeautifulSoup(markdown_with_math(source)[0], 'html.parser')
    cells = tree.select('table tbody td')
    assert len(cells) == 3
    assert cells[0].select_one('.math-inline')
    assert cells[1].code.get_text() == 'a|b' and 'A|B' in cells[1].get_text()
    assert cells[2].strong.get_text() == '82.72'
    assert '后续普通文字' not in tree.table.get_text()


def test_file_report_math_assets_resolve_to_existing_local_files(tmp_path):
    from urllib.parse import urljoin, urlsplit
    from urllib.request import url2pathname
    from html.parser import HTMLParser
    class Assets(HTMLParser):
        def __init__(self):
            super().__init__()
            self.paths = []
        def handle_starttag(self, tag, attrs):
            attrs = dict(attrs)
            if tag == 'script' and attrs.get('src'):
                self.paths.append(attrs['src'])
            if tag == 'link' and attrs.get('href'):
                self.paths.append(attrs['href'])
    target = tmp_path / 'report.html'
    render_report(r'# Test' + '\n\n' + r'像素为 \(512 \times 512\)。', target, 'Test')
    document = target.read_text(encoding='utf-8')
    assert 'class="math-inline"' in document and r'512 \times 512' in document
    parser = Assets()
    parser.feed(document)
    assert any('katex.min.js' in p for p in parser.paths)
    for reference in parser.paths:
        resolved = urlsplit(urljoin(target.as_uri(), reference))
        assert resolved.scheme == 'file'
        path = Path(url2pathname(resolved.path))
        assert path.is_file(), f'Local report asset is missing: {path}'


def test_report_presentation_omits_verbose_comparison_audit_section():
    from arxiv_ra.render import report_document
    text = '# Compare\n\n## 比较矩阵\nKeep matrix\n\n## 条目依据与待核对原因\nVerbose detail\n### P1\nQuote\n\n## 固定证据摘录\nKeep sources'
    document = report_document(text, 'Compare')
    assert '条目依据与待核对原因' not in document and 'Verbose detail' not in document
    assert 'Keep matrix' in document and 'Keep sources' not in document
    assert 'src="/static/vendor/katex/katex.min.js"' in document
    assert 'src="/static/report.js?v=' in document


def test_legacy_comparison_links_survive_without_repeated_source_text():
    from arxiv_ra.render import report_document
    text = '''# Compare

## 比较矩阵
[P1:Q1](#p1-q1) [P1:R](#p1-r) [P1:A](#p1-a)

## 固定证据摘录
<h3 id="p1-q1">P1:Q1</h3>

[PDF 第 2 页](../../day/paper.pdf#page=2)

> Repeated quote

<h3 id="p1-r">P1:R</h3>

[打开所用阅读报告](../../day/report.html)

> 原陈述：\\#\\#\\# 1. **long repeated claim**

<h3 id="p1-a">P1:A</h3>

> Repeated abstract
'''
    document = report_document(text, 'Compare')
    assert '固定证据摘录' not in document and '原陈述' not in document
    assert 'Repeated quote' not in document and 'Repeated abstract' not in document
    assert 'href="../../day/paper.pdf#page=2"' in document
    assert 'href="../../day/report.html"' in document
    assert 'href="sources.json"' in document
    assert 'href="#p1-' not in document


def test_legacy_numeric_audit_keeps_warning_without_duplicating_markdown_claim():
    from arxiv_ra.render import report_document
    text = '''# Paper

> **实验数值待核对**：1 个条目缺少对应原文依据。请先查看文末“实验数值核对”，再使用这些结果。

## 关键结果
### 1. 质量排名
**评测分析**：13.9%

## 原文依据与覆盖
已定位 2 条摘录。

### 实验数值核对
本次检查 2 个数值条目，发现 1 个待核对条目；未提示不代表结论正确。

- **数值待核对 · 关键结果**：13.9%。原陈述：\\#\\#\\# 1. **评测分析**

### 原文 1
[PDF 第 2 页](paper.pdf#page=2)
> repeated quote
'''
    document = report_document(text, 'Paper')
    assert '原陈述' not in document and 'repeated quote' not in document
    assert '<strong>评测分析</strong>' in document and '质量排名</h3>' in document
    assert '实验数值待核对' in document and 'href="evidence.json"' in document


def test_markdown_math_preserves_display_latex() -> None:
    source = r"""## 核心方法

\[
V \xrightarrow{\psi} C \xrightarrow{\text{render}} \tilde V
\]
"""

    body, toc = markdown_with_math(source)

    assert 'class="math-block"' in body
    assert r"\xrightarrow{\psi}" in body
    assert "核心方法" in toc
    assert "<p>[" not in body


def test_render_report_includes_local_katex_and_sidebar_navigation(tmp_path: Path) -> None:
    destination = tmp_path / "report.html"

    render_report(
        "# Paper\n\n## 核心方法\n\n内容",
        destination,
        "Paper",
        arxiv_id="2407.05600",
    )

    page = destination.read_text(encoding="utf-8")
    assert "/static/vendor/katex/katex.min.js" in page
    assert "/static/vendor/katex/katex.min.css" in page
    assert BeautifulSoup(page, 'html.parser').select_one('.report-sidebar') is not None
    assert 'aria-label="报告目录"' in page
    assert "返回报告库" in page
    assert 'id="report-library-add"' in page
    assert 'data-arxiv-id="2407.05600"' in page
    script = (Path(__file__).parents[1] / "src/arxiv_ra/static/report.js").read_text(encoding="utf-8")
    assert "/static/report.js" in page
    assert "/api/library/status" in script
    assert "/api/library/add" in script
    assert "scroll-padding-top:24px" in page
    assert "position:relative;z-index:10" in page
    assert "position:sticky;top:18px" in page
    assert "max-width:1920px" in page
    assert "grid-template-columns:270px minmax(0,1fr)" in page
    assert "window.scrollTo(0,0)" in script
