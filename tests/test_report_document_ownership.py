from arxiv_ra.evidence import attach_evidence, source_table_inventory, report_table_coverage
from arxiv_ra.models import Paper, VerifiedMetadata, ParsedPaper
from arxiv_ra.report_metadata import protect_metadata
from arxiv_ra.report_tables import ReportTables


def test_reference_column_typography_cannot_block_publication():
    note = '### Table 5: Scores\n| Method | Score | 原文依据 (证据 ID) |\n|---|---|---|\n| A | 91.2 | （片段引用不可用，缺少可定位原文依据） |'
    tables = ReportTables.from_notes([note], [{'number': 5}])
    raw = tables.render('## 关键结果\n\n[[表格:table-5]]')
    final, _ = attach_evidence(raw, ParsedPaper('Sparse source', ['Sparse source']), pdf_available=True, full_report=True)
    assert tables.review(final)['status'] == 'passed'
    assert tables.review(final.replace('91.2', '9.12'))['status'] == 'failed'


def test_metadata_accepts_bullets_but_emits_one_complete_table():
    paper = Paper.from_dict({'arxiv_id':'2603.29852', 'title':'VectorGym', 'authors':[{'name':'Correct Author'}]})
    raw = '# Wrong\n\n| 字段 | 内容 |\n|---|---|\n| 中文标题 | 中文标题值 |\n\n- 作者：Wrong Author\n- 机构（来自论文首页节选）：Institute\n  and Lab\n- 修订日期：2099-01-01\n\n> 阅读提示应保留。\n\n## 关键结果\n\n- 作者认为方法有效。'
    final = protect_metadata(raw, paper, VerifiedMetadata())
    assert '- 作者：' not in final and 'Wrong Author' not in final and '2099-01-01' not in final
    assert '| 机构（论文摘录，未核实） | Institute and Lab |' in final
    assert '阅读提示应保留' in final and '- 作者认为方法有效。' in final
    assert protect_metadata(final, paper, VerifiedMetadata()) == final
    empty = protect_metadata('# Title\n\n## 关键结果\nText', paper, VerifiedMetadata())
    rows = lambda text: [line.split('|')[1].strip() for line in text.splitlines() if line.startswith('|')]
    assert rows(empty) == rows(final)


def test_model_metadata_pipe_is_one_cell_and_roundtrips():
    from arxiv_ra.render import markdown_with_math
    from bs4 import BeautifulSoup
    paper = Paper.from_dict({'arxiv_id':'1234.56789', 'title':'Test'})
    for front in ['- 中文标题：A | B\n- 机构：Lab | Institute',
                  '| 字段 | 内容 |\n|---|---|\n| 中文标题 | A | B |\n| 机构 | Lab | Institute |']:
        result = protect_metadata('# Test\n\n'+front+'\n\n## 关键结果\nText',paper,VerifiedMetadata())
        body,_ = markdown_with_math(result)
        tree = BeautifulSoup(body,'html.parser')
        assert 'A | B' in tree.get_text() and 'Lab | Institute' in tree.get_text()
        assert all(len(row.find_all('td',recursive=False))==2 for row in tree.select('tbody tr'))
        assert protect_metadata(result,paper,VerifiedMetadata())==result


def test_visual_tables_are_a_distinct_coverage_kind():
    parsed = ParsedPaper('source', ['Table 8: SVG Editing qualitative examples. Results from models.'])
    inventory = source_table_inventory(parsed)
    assert inventory[0]['kind'] == 'visual'
    coverage = report_table_coverage('## 关键结果\nText', inventory)
    assert not coverage['missing']
    assert coverage['visual'][0]['number'] == 8


def test_column_roles_are_explicit_and_old_catalogues_remain_readable():
    import hashlib
    import json
    from arxiv_ra.table_schema import column_role
    for header in ['原文依据 (证据 ID)', '**原文依据（证据ID）**', '支持依据（摘录）', '依据']:
        assert column_role(header) == 'reference'
    for header in ['Evidence Score', '依据准确率', '原文依据(Score)', 'CLIP ↑']:
        assert column_role(header) == 'data'
    tables = ReportTables.from_notes(['### Table 1: Scores\n| Model | Score | 原文依据 (证据 ID) |\n|---|---|---|\n| A | 91.2 | none |'], [{'number':1}])
    stored = tables.to_dict()
    assert stored['tables'][0]['variants'][0]['column_roles'] == ('data', 'data', 'reference')
    legacy = {k:v for k,v in stored.items() if k != 'sha256'}
    legacy['version'] = 1
    for table in legacy['tables']:
        for variant in table['variants']:
            del variant['column_roles']
    legacy['sha256'] = hashlib.sha256(json.dumps(legacy, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    assert ReportTables.from_dict(legacy) == tables


def test_source_page_display_preserves_visuals_once_per_page(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from arxiv_ra.report_publication import present_source_pages
    from arxiv_ra.render import markdown_with_math
    from bs4 import BeautifulSoup
    import fitz
    class PDF:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def __len__(self): return 28
        def __getitem__(self, index):
            assert index == 26
            return SimpleNamespace(get_pixmap=lambda **kwargs: SimpleNamespace(save=lambda path: path.write_bytes(b'preview')))
    monkeypatch.setattr(fitz, 'open', lambda path: PDF())
    pdf = tmp_path / 'paper.pdf'
    pdf.write_bytes(b'source')
    coverage = {'visual':[{'number':n,'page':27,'kind':'visual','title':'SVG Editing qualitative examples'} for n in [8,9]], 'missing':[]}
    original = '## 关键结果\n\n### 编辑效果\n分析。\n\n## 局限性\n限制。'
    report, displays = present_source_pages(original, coverage, pdf)
    assert report.index('paperloom-source-page:27') < report.index('## 局限性')
    assert report.count('![Table') == 1 and displays[0]['numbers'] == [8,9]
    assert present_source_pages(report, coverage, pdf)[0] == report
    body,toc = markdown_with_math(report)
    tree=BeautifulSoup(body,'html.parser')
    assert len(tree.select('details img')) == 1 and 'Table 8' not in toc


def test_metadata_layout_is_full_width_without_changing_result_matrices():
    from arxiv_ra.render import report_document, REPORT_STYLE
    from bs4 import BeautifulSoup
    paper = Paper.from_dict({'arxiv_id':'2603.29852','title':'VectorGym'})
    report = protect_metadata('# Test\n\n## 关键结果\n\n| Model | Score |\n|---|---|\n| A | 91.2 |',paper,VerifiedMetadata())
    html = BeautifulSoup(report_document(report,paper.title),'html.parser')
    assert len(html.select('table.report-metadata')) == 1
    assert len(html.select('table:not(.report-metadata)')) == 1
    assert 'table.report-metadata{display:table;width:100%;table-layout:fixed}' in REPORT_STYLE


def test_evidence_only_rewrites_reference_fields_not_data_text():
    placeholder = '（片段引用不可用，缺少可定位原文依据）'
    note = f'### Table 1: Results\n| Method | Explanation | Score | 原文依据 (证据 ID) |\n|---|---|---|---|\n| A | {placeholder} | 91.2 [[证据ID:unknown]] | {placeholder} |'
    catalogue = ReportTables.from_notes([note], [{'number':1}])
    final,_ = attach_evidence(catalogue.render('## 关键结果\n\n[[表格:table-1]]'),ParsedPaper('source',['source']),pdf_available=True,full_report=True)
    assert catalogue.review(final)['status'] == 'passed'
    assert placeholder in final and '91.2' in final
