"""Source IDs survive lossy model notes without weakening legacy quote checks."""
import json
import re
from types import SimpleNamespace

from arxiv_ra.config import LLMConfig
from arxiv_ra.evidence import attach_evidence
from arxiv_ra.models import Author, Paper, ParsedPaper, VerifiedMetadata
from arxiv_ra.report import ReportGenerator, finalize_report_structure
from arxiv_ra.report_metadata import protect_metadata
from arxiv_ra.source_spans import source_spans, span_batches, cited_span_material


SOURCE = "Model CLIP DINOv2 MSE Invalid\nBaseline – no edit 0.9634 0.9011 10488 0\nGPT-4o mini 0.9040 0.8058 8526 14"


def paper():
    return Paper.from_dict({'arxiv_id': '2506.15903', 'title': 'VectorEdits', 'version': 1,
                            'authors': [{'name': 'Marek Kadlčík'}], 'metadata_source': 'arxiv',
                            'metadata_status': 'complete', 'categories': ['cs.LG']})


def test_ids_are_deterministic_page_bound_and_invalidated_by_changed_text():
    parsed = ParsedPaper('parser text', ['title ' * 100, SOURCE, ''])
    spans = source_spans(parsed)
    assert spans == source_spans(parsed)
    for span in spans.values():
        assert parsed.page_texts[span['page'] - 1][span['start']:span['end']] == span['quote']
        assert len(span['quote']) <= 500
    assert set(spans).isdisjoint(source_spans(ParsedPaper('', ['title ' * 100, SOURCE + ' changed', ''])))
    # Distributing PDF evidence does not equate parser chunks to page offsets.
    batches = span_batches(spans, 3)
    assert [s for b in batches for s in b] == list(spans.values())


def test_id_attaches_exact_source_and_numeric_audit_still_rejects_wrong_value():
    parsed = ParsedPaper(SOURCE, [SOURCE])
    key = next(iter(source_spans(parsed)))
    raw = f'## 关键结果\n\n| 模型 | MSE | 依据 |\n| --- | --- | --- |\n| GPT-4o mini | 8526 | [[证据ID:{key}]] |\n| wrong | 8527 | [[证据ID:{key}]] |'
    report, evidence = attach_evidence(raw, parsed, pdf_available=True, full_report=True)
    citation = evidence['citations'][0]
    assert citation['source_id'] == key and citation['page'] == 1
    assert citation['quote'] == ' '.join(SOURCE.split())
    assert citation['start'] == 0 and citation['end'] == len(SOURCE)
    assert evidence['source_spans']['cited'] == 1
    assert [x['numbers'] for x in evidence['numeric_audit']['issues']] == [['8527']]
    assert '[[证据ID:' not in report and 'paper.pdf#page=1' in report


def test_repeated_page_text_can_be_anchored_by_id_but_not_legacy_quote():
    parsed = ParsedPaper('', [SOURCE, SOURCE])
    key = next(k for k, s in source_spans(parsed).items() if s['page'] == 2)
    report, evidence = attach_evidence(f'[[证据:{SOURCE}]]\n[[证据ID:{key}]]', parsed,
                                       pdf_available=True, full_report=True)
    assert evidence['rejected_citations'] == 1
    assert evidence['cited_pages'] == [2]
    assert 'paper.pdf#page=1' not in report


def test_unknown_stale_and_unavailable_ids_never_get_links():
    parsed = ParsedPaper('', [SOURCE])
    key = next(iter(source_spans(parsed)))
    for selected, available, full in [(parsed, False, True), (parsed, True, False),
                                      (ParsedPaper('', [SOURCE + ' changed']), True, True)]:
        report, evidence = attach_evidence(f'[[证据ID:{key}]] [[证据ID:invented]]', selected,
                                           pdf_available=available, full_report=full)
        assert not evidence['citations'] and evidence['rejected_citations'] == 2
        assert 'paper.pdf#page=' not in report


def test_numbered_subheading_is_not_a_result_but_unsupported_calculation_is():
    parsed = ParsedPaper('', [SOURCE])
    key = next(iter(source_spans(parsed)))
    _, evidence = attach_evidence(f'## 关键结果\n\n### 2. 结果\nMSE 8526 [[证据ID:{key}]]\n\n比例 13.9% [[证据ID:{key}]]',
                                  parsed, pdf_available=True, full_report=True)
    assert [i['numbers'] for i in evidence['numeric_audit']['issues']] == [['13.9%']]


def test_synthesis_restores_literal_text_and_bounds_selected_bank():
    parsed = ParsedPaper('', [('Page ' + str(i) + ' hyphen-\nated text ' * 80) for i in range(100)])
    spans = source_spans(parsed)
    notes = [' '.join(f'[[证据ID:{key}]]' for key in spans) + ' [[证据ID:invented]]']
    cleaned, material = cited_span_material(notes, spans)
    selected = json.loads(material)
    assert sum(len(s['text']) for s in selected) <= 32000
    assert '[[证据ID:invented]]' not in cleaned[0]
    assert 'hyphen-\nated' in selected[0]['text']
    assert all(s['text'] == spans[s['id']]['quote'] for s in selected)


def test_synthesis_budget_keeps_late_notes_and_their_formula_sources():
    pages = [('Page ' + str(i) + ' original evidence ' * 100) for i in range(40)]
    spans = source_spans(ParsedPaper('', pages))
    keys = list(spans)
    early = ' '.join(f'[[证据ID:{k}]]' for k in keys[:100])
    late = ' '.join(f'[[证据ID:{k}]]' for k in keys[100:])
    formula_key = keys[-1]
    late += '\n\n' + r'\[rMSE = \sqrt{1-x}\]' + f'\n[[证据ID:{formula_key}]]'
    cleaned, material = cited_span_material([early, late], spans)
    selected = {s['id']: s['text'] for s in json.loads(material)}
    assert formula_key in selected
    assert any(k in selected for k in keys[:100])
    assert any(k in selected for k in keys[100:-1])
    assert sum(map(len, selected.values())) <= 32000
    assert {m[1] for note in cleaned for m in re.finditer(r'\[\[证据ID:([^]]+)\]\]', note)} == set(selected)


def test_formula_priority_does_not_promote_unknown_ids_or_execute_source_text():
    spans = source_spans(ParsedPaper('', ['Literal source ' * 40]))
    cleaned, material = cited_span_material([r'\[x=1\] [[证据ID:invented]]'], spans)
    assert json.loads(material) == [] and '[[证据ID:' not in cleaned[0]


def test_long_first_page_cannot_crowd_out_table_on_next_page_of_same_note():
    spans = source_spans(ParsedPaper('', ['Early prose ' * 8000, 'Table results ' * 40]))
    notes = [' '.join(f'[[证据ID:{k}]]' for k in spans)]
    _, material = cited_span_material(notes, spans)
    assert {spans[s['id']]['page'] for s in json.loads(material)} == {1, 2}


def test_production_generation_carries_ids_into_final_report_and_protects_names():
    parsed = ParsedPaper(SOURCE, [SOURCE])
    key = next(iter(source_spans(parsed)))
    calls = []
    def chat(system, user):
        calls.append(user)
        if len(calls) == 1:
            return f'GPT-4o mini MSE 8526 [[证据ID:{key}]]'
        assert key in user and 'GPT-4o mini 0.9040 0.8058 8526 14' in user
        return f'# Wrong title\n\n| 字段 | 内容 |\n| --- | --- |\n| 作者 | Marek K駆lcík |\n| 机构 | Masaryk University |\n\n## 关键结果\nGPT-4o mini MSE 8526 [[证据ID:{key}]]'
    raw = ReportGenerator(SimpleNamespace(enabled=True, chat=chat), LLMConfig()).generate(paper(), VerifiedMetadata(), parsed, None)
    final, evidence = attach_evidence(finalize_report_structure(raw, None), parsed, pdf_available=True, full_report=True)
    assert len(calls) == 2
    assert 'Marek Kadlčík' in final and 'K駆lcík' not in final
    assert '机构（论文摘录，未核实） | Masaryk University' in final
    assert final.startswith('# VectorEdits')
    assert evidence['validated_citations'] == 1 and not evidence['numeric_audit']['issues']


def test_structured_front_matter_handles_missing_authors_and_combined_model_row():
    model = '# Wrong\n\n## 基本信息表\n\n| 字段 | 内容 |\n| --- | --- |\n| 作者及机构 | Invented / Fake |\n| 首次公开日期 | 2099-01-01 |\n\n## 核心方法\n原报告的方法与作者评论保持。'
    metadata = VerifiedMetadata(authors=[Author('Marek Kadlcik', ['Known institute'])])
    result = protect_metadata(model, paper(), metadata)
    assert 'Marek Kadlčík' in result and 'Marek Kadlcik' not in result
    assert 'Invented' not in result and '2099-01-01' not in result
    assert 'Known institute' in result and '原报告的方法与作者评论保持。' in result
    p = paper()
    p.authors = []
    result = protect_metadata(model, p, metadata)
    assert 'Marek Kadlcik' in result
    assert '| 作者 | 未核实 |' in protect_metadata(model, p, VerifiedMetadata())


def test_metadata_values_cannot_create_links_html_or_extra_table_columns():
    p = paper()
    p.authors = [Author('<img src=x> A|B [name](https://invalid.test)')]
    result = protect_metadata('# Title\n\n## 核心方法\nBody', p, VerifiedMetadata())
    assert '<img' not in result and '|B' not in result.replace('\\|', '')
    assert '[name](' not in result


def test_page_only_source_changes_invalidate_checkpoint_prompts(tmp_path):
    from unittest.mock import Mock
    from arxiv_ra.config import AppConfig
    from arxiv_ra.report_checkpoint import ReportCheckpoint, bind_report_checkpoint
    config = AppConfig(output_dir=str(tmp_path), profile_id='test')
    llm = SimpleNamespace(enabled=True, chat=Mock(return_value='# Paper\n\n## 核心方法\nBody'), client=None)
    generator = ReportGenerator(llm, config.llm)
    checkpoint = ReportCheckpoint(tmp_path, config, paper())
    with bind_report_checkpoint(checkpoint):
        generator.generate(paper(), VerifiedMetadata(), ParsedPaper(SOURCE, [SOURCE]), None)
        assert llm.chat.call_count == 2
        generator.generate(paper(), VerifiedMetadata(), ParsedPaper(SOURCE, [SOURCE]), None)
        assert llm.chat.call_count == 2
        # Parser Markdown is identical, but changed physical-page text must not
        # reuse a chunk response carrying IDs from the former page snapshot.
        generator.generate(paper(), VerifiedMetadata(), ParsedPaper(SOURCE, [SOURCE + ' changed']), None)
        assert llm.chat.call_count >= 3


def test_real_pdf_pipeline_persists_id_locations_for_comparison(tmp_path):
    import fitz
    from arxiv_ra.config import AppConfig, PDFConfig
    from arxiv_ra.pdf_pipeline import PDFParser
    from arxiv_ra.pipeline import DailyPipeline
    from arxiv_ra.utils import read_json
    from arxiv_ra.comparison import ComparisonService
    source = tmp_path / 'source.pdf'
    with fitz.open() as pdf:
        pdf.new_page().insert_text((50, 50), 'GPT-4o mini MSE 8526 on the vector editing benchmark.', fontsize=9)
        pdf.save(source)
    config = AppConfig(output_dir=str(tmp_path / 'run'), profile_id='test')
    config.obsidian.enabled = False
    config.pdf = PDFConfig(parser='pymupdf', use_docling_if_available=False)
    parser = PDFParser(config.pdf)
    import re
    def chat(system, user):
        key = re.search(r'S1-[a-f0-9]{24}-\d+-\d+-\d+', user)[0]
        return f'# Paper\n\n## 关键结果\nMSE 8526 [[证据ID:{key}]]'
    llm = SimpleNamespace(enabled=True, chat=chat)
    clients = SimpleNamespace(llm=llm, reporter=ReportGenerator(llm, config.llm), parser=parser,
                              verifier=SimpleNamespace(verify=lambda p: VerifiedMetadata()),
                              arxiv_html=SimpleNamespace(fetch=lambda *a: []))
    papers = [paper(), paper()]
    papers[1].arxiv_id = '2502.19453'
    for p in papers:
        result = DailyPipeline(config, tmp_path, clients=clients)._process_paper(p, tmp_path / 'run/2026-09-25', False, local_pdf=source)
        evidence = read_json(result.report_path.with_name('evidence.json'))
        assert evidence['citations'][0]['source_id'].startswith('S1-')
        assert evidence['source_spans']['cited'] == 1
    snapshot = ComparisonService(config, tmp_path, clients=SimpleNamespace(llm=None)).prepare(['2506.15903v1', '2502.19453v1'])
    for entry in snapshot['sources']:
        quotes = [q for q in entry['evidence'] if q['kind'] == 'quote']
        assert len(quotes) == 1 and quotes[0]['page'] == 1 and '8526' in quotes[0]['text']
        assert quotes[0]['source_id'].startswith('S1-') and quotes[0]['start'] == 0
        assert len(quotes[0]['text_sha256']) == 64
