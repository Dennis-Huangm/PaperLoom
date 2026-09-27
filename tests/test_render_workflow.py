"""Contracts for unattended report rendering, not hand-edited HTML fixtures."""
from pathlib import Path

import pytest
from bs4 import BeautifulSoup

from arxiv_ra.render import markdown_with_math, render_report


@pytest.mark.parametrize('delimiters', [('$$', '$$'), ('$', '$'), (r'\[', r'\]')])
@pytest.mark.parametrize('wrapped', [True, False])
def test_resolved_evidence_inside_formula_becomes_link_outside_math(delimiters, wrapped):
    from arxiv_ra.evidence import attach_evidence
    from arxiv_ra.models import ParsedPaper
    quote = 'The reward is the weighted sum of the six criterion scores.'
    marker = '[[证据:' + quote + ']]'
    suffix = r'\quad \text{' + marker + '}' if wrapped else ' ' + marker
    source = '## 核心方法\n\n' + delimiters[0] + r'R=\sum_k w_k s_k' + suffix + delimiters[1]
    parsed = ParsedPaper(text=quote, page_texts=[quote], total_pages=1, figures=[], parser='fixture')
    report, evidence = attach_evidence(source, parsed, pdf_available=True, full_report=True)
    tree = BeautifulSoup(markdown_with_math(report)[0], 'html.parser')
    formula = tree.select_one('.math-block,.math-inline')
    assert formula.get_text().strip() == r'R=\sum_k w_k s_k'
    link = tree.select_one('a[href="paper.pdf#page=1"]')
    assert link is not None and link.find_parent(class_=['math-block', 'math-inline']) is None
    assert evidence['validated_citations'] == 1


def test_formula_citation_repair_keeps_math_text_and_code_literal():
    source = r'''$$x=1 \text{单位：[原文 2 · PDF 第 3 页](paper.pdf#page=3)}$$

`$x [原文 2](paper.pdf#page=3)$`

$$y=2 \text{[unsafe](javascript:alert(1))}$$
'''
    tree = BeautifulSoup(markdown_with_math(source)[0], 'html.parser')
    assert tree.select_one('.math-block').get_text() == r'x=1 \text{单位：}'
    assert len(tree.select('a[href="paper.pdf#page=3"]')) == 1
    assert tree.code.get_text() == '$x [原文 2](paper.pdf#page=3)$'
    assert not tree.select('a[href^="javascript:"]')


@pytest.mark.parametrize('marker', ['-', '*', '+', '1.', '1)'])
def test_lists_interrupt_citation_paragraphs(marker):
    source = f'[原文 4](paper.pdf#page=19)\n{marker} **符号定义**：定义。\n{marker} **计算逻辑**：解释。'
    tree = BeautifulSoup(markdown_with_math(source)[0], 'html.parser')
    assert len(tree.select('li')) == 2
    assert tree.a['href'] == 'paper.pdf#page=19'
    assert tree.a.find_parent('li') is None


def test_nested_lists_quotes_and_code_are_structural():
    source = '''说明：
- 第一层
  - 第二层
    1. 第三层
    2. 另一步
- 下一项

> 引文：
> - 引文中的列表

```markdown
正文：
- 字面列表
\\(literal\\)
```

行内代码 `- literal \\(x\\)`，负数 -3、GPT-4o、普通 - 连字符。
'''
    tree = BeautifulSoup(markdown_with_math(source)[0], 'html.parser')
    assert len(tree.select('ul > li > ul > li > ol > li')) == 2
    assert len(tree.select('blockquote ul li')) == 1
    assert '\\(literal\\)' in tree.select_one('pre code').get_text()
    assert tree.select('code')[-1].get_text() == '- literal \\(x\\)'


def test_automatic_render_mixed_model_output(tmp_path: Path):
    source = r'''# 自动报告
## 方法
### 相对均方误差
\[
rMSE = \sqrt{1-\min(1,x)}
\]
[原文 4](paper.pdf#page=19)
- **符号定义**：\(x\) 为相对误差。
- **计算逻辑**：误差趋近 0 时得分趋近 1。
### 路径结构
解释：
- **宏观对齐**：权重 0.6。
- **微观对应**：权重 0.4。
  - 保留父子关系。
## 结果
结果如下：
| 模型 | 得分 |
| --- | ---: |
| A | 82.72 |
后续说明。
'''
    target = tmp_path / 'report.html'
    render_report(source, target, '自动报告')
    tree = BeautifulSoup(target.read_text(encoding='utf-8'), 'html.parser')
    article = tree.article
    assert len(article.select('li')) == 5
    assert len(article.select('ul > li > ul > li')) == 1
    assert len(article.select('.math-block')) == 1
    assert len(article.select('.math-inline')) == 1
    assert len(article.select('table tbody tr')) == 1
    assert article.select_one('td[align=right]').get_text() == '82.72'
    assert '后续说明' not in article.table.get_text()
    for link in tree.select('nav a[href^="#"]'):
        assert tree.find(id=link['href'][1:]) is not None


def test_math_and_code_pipes_do_not_split_table_cells():
    source = r'''| 条件 | 代码 | 得分 |
| --- | --- | ---: |
| \(P(a|b)\) | `a|b` | $x+1$ |
'''
    tree = BeautifulSoup(markdown_with_math(source)[0], 'html.parser')
    assert len(tree.select('td')) == 3
    assert tree.code.get_text() == 'a|b'
    assert [s.get_text() for s in tree.select('.math-inline')] == ['P(a|b)', 'x+1']


def test_render_gate_rejects_lost_lists_before_replacing_previous_html(tmp_path, monkeypatch):
    from arxiv_ra import markdown_rendering as engine
    target = tmp_path / 'report.html'
    target.write_text('previous valid report', encoding='utf-8')
    normal = engine._parser
    monkeypatch.setattr(engine, '_parser', lambda: normal().disable('list'))
    with pytest.raises(engine.ReportRenderError, match='列表或表格未正确解析'):
        render_report('说明：\n- **定义**：内容。', target, 'Test')
    assert target.read_text(encoding='utf-8') == 'previous valid report'


def test_render_gate_rejects_structures_removed_during_sanitization(tmp_path, monkeypatch):
    import re
    import arxiv_ra.render as renderer
    from arxiv_ra.markdown_rendering import ReportRenderError
    normal = renderer.nh3.clean
    monkeypatch.setattr(renderer.nh3, 'clean', lambda value, **kw:
                        re.sub(r'</?li\b[^>]*>', '', normal(value, **kw)))
    target = tmp_path / 'report.html'
    with pytest.raises(ReportRenderError, match='li 结构丢失'):
        render_report('说明：\n- **定义**：内容。', target, 'Test')
    assert not target.exists()


def test_render_gate_prevents_table_cell_data_loss(tmp_path):
    from arxiv_ra.markdown_rendering import ReportRenderError
    target = tmp_path / 'report.html'
    with pytest.raises(ReportRenderError, match='列数超过表头'):
        render_report('| A | B |\n| --- | --- |\n| 1 | 2 | 3 |', target, 'Test')
    assert not target.exists()


def test_literals_images_html_safety_and_duplicate_heading_navigation():
    source = r'''# 标题
## 方法
\- 字面符号，`- 代码`，负数 -3，GPT-4o。
![方法图](method-figure-01.png)
<script>alert(1)</script>
<img src="x" onerror="alert(1)">

## 方法
7. 第七步
8. 第八步
'''
    body, toc = markdown_with_math(source)
    tree = BeautifulSoup(body, 'html.parser')
    assert not tree.script and all(not i.has_attr('onerror') for i in tree.select('img'))
    assert tree.select_one('img[alt="方法图"]')['src'] == 'method-figure-01.png'
    assert tree.ol['start'] == '7'
    assert len({h['id'] for h in tree.select('h2')}) == 2
    assert all(tree.find(id=a['href'][1:]) for a in BeautifulSoup(toc, 'html.parser').select('a'))


def test_pipeline_automatically_renders_model_lists_and_stops_on_structure_loss(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import Mock
    import fitz
    from arxiv_ra.config import AppConfig, PDFConfig
    from arxiv_ra.models import Paper, VerifiedMetadata
    from arxiv_ra.pdf_pipeline import PDFParser
    from arxiv_ra.pipeline import DailyPipeline
    from arxiv_ra.report import ReportGenerator
    from arxiv_ra import markdown_rendering as engine

    source = tmp_path / 'source.pdf'
    with fitz.open() as pdf:
        pdf.new_page().insert_text((50, 50), 'A local test paper for rendering.')
        pdf.save(source)
    config = AppConfig(output_dir=str(tmp_path / 'run'), profile_id='render-test')
    config.obsidian.enabled = False
    config.pdf = PDFConfig(parser='pymupdf', max_pages=1, use_docling_if_available=False)
    llm = SimpleNamespace(enabled=True, chat=Mock(return_value=r'''# Test
## 核心方法
\[rMSE = \sqrt{1-x}\]
方法说明：
- **符号定义**：\(x\) 为误差。
- **计算逻辑**：逐步计算。
'''))
    clients = SimpleNamespace(llm=llm, reporter=ReportGenerator(llm, config.llm), parser=PDFParser(config.pdf),
        verifier=SimpleNamespace(verify=lambda p: VerifiedMetadata(title=p.title)),
        arxiv_html=SimpleNamespace(fetch=lambda *a: []))
    paper = Paper('2407.05600', 'Test', [], 'Abstract', [], '', None, None, '', '', version=2)
    pipeline = DailyPipeline(config, tmp_path, clients=clients)
    artifact = pipeline._process_paper(paper, tmp_path / 'run/valid', False, local_pdf=source)
    tree = BeautifulSoup(artifact.report_path.with_suffix('.html').read_text(encoding='utf-8'), 'html.parser')
    assert any('符号定义' in li.get_text() for li in tree.select('li'))
    assert len(tree.select('.math-block')) == 1
    normal = engine._parser
    monkeypatch.setattr(engine, '_parser', lambda: normal().disable('list'))
    failed = tmp_path / 'run/invalid'
    with pytest.raises(engine.ReportRenderError):
        pipeline._process_paper(paper, failed, False, local_pdf=source)
    assert list(failed.rglob('report.md'))  # Reusable generated content is kept.
    assert not list(failed.rglob('report.html'))
    assert not list(failed.rglob('metadata.json'))  # No successful artifact published.


def test_historical_report_check_failure_has_a_readable_local_error(tmp_path):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from arxiv_ra.web_artifacts import ReportStaticFiles
    bad = '| A | B |\n| --- | --- |\n| 1 | 2 | 3 |'
    (tmp_path / 'report.md').write_text(bad, encoding='utf-8')
    (tmp_path / 'report.html').write_text('old broken view', encoding='utf-8')
    app = FastAPI()
    app.mount('/artifacts', ReportStaticFiles(directory=tmp_path))
    with TestClient(app) as client:
        result = client.get('/artifacts/report.html')
        assert result.status_code == 422 and '报告排版检查未通过' in result.text
        assert 'old broken view' not in result.text
        assert client.get('/artifacts/report.md').content == (tmp_path / 'report.md').read_bytes()


def test_comparison_matrix_layout_keeps_paper_columns_readable():
    source = '''| 维度 | P1 | P2 |
| --- | --- | --- |
| 方法 | 第一篇的说明 | 第二篇的说明 |'''
    tree = BeautifulSoup(markdown_with_math(source)[0], 'html.parser')
    assert tree.table['class'] == ['comparison-matrix']
    assert [cell.get_text() for cell in tree.select('td')] == ['方法', '第一篇的说明', '第二篇的说明']
    ordinary = BeautifulSoup(markdown_with_math(source.replace('维度', '模型'))[0], 'html.parser')
    assert not ordinary.table.get('class')
