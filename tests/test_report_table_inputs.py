from arxiv_ra.config import LLMConfig
from arxiv_ra.models import ParsedPaper
from arxiv_ra.report import ReportGenerator


def test_chunk_boundary_preserves_table_caption_headers_and_every_row():
    caption = 'Table 11: Scores by task and difficulty\n\n'
    table = '| Model | Score |\n|---|---|\n' + ''.join(f'| model-{i} | {i}.25 |\n' for i in range(170))
    text = 'Background ' * 250 + '\n\n' + caption + table + '\n\nDiscussion'
    chunks = ReportGenerator(None, LLMConfig(max_chunk_chars=4000))._chunks(text)
    assert ''.join(chunks) == text
    assert any(caption + table in chunk for chunk in chunks)


def test_longer_than_target_table_is_an_atomic_input_without_lost_rows():
    table = 'Table 8: Results\n\n| Model | Score |\n|---|---|\n' + ''.join(
        f'| model-{i} | {i}.25 |\n' for i in range(400))
    text = 'Introduction\n\n' + table + '\n\nDiscussion'
    chunks = ReportGenerator(None, LLMConfig(max_chunk_chars=4000))._chunks(text)
    assert ''.join(chunks) == text
    assert any(table in chunk for chunk in chunks)


def test_docling_table_chunk_receives_bounded_original_page_evidence():
    pages = ['Table 11: Scores\nMethod\nTask A\nTask B\nModel A\n1.25\n2.50\n' + 'Original page text ' * 500,
             'Other page ' * 400]
    parsed = ParsedPaper('Table 11: Scores\n\n| Method | wrong columns |\n|---|---|\n| A | 1.252.50 |',
                         pages, parser='docling')
    _, banks, spans = ReportGenerator(None, LLMConfig(max_chunk_chars=4000)).analysis_inputs(parsed)
    original_ids = {key for key, value in spans.items() if value['page'] == 1}
    assert {span['source_id'] for span in banks[0]} <= original_ids
    assert 'Table 11: Scores' in banks[0][0]['quote']
    assert sum(len(span['quote']) for span in banks[0]) <= 4000


def test_prose_table_reference_does_not_attach_an_unrelated_page():
    parsed = ParsedPaper('Discussion cites Table 11: without a caption.',
                         ['Table 11: Scores\n' + 'Original table text ' * 300, 'Other page ' * 400])
    generator = ReportGenerator(None, LLMConfig(max_chunk_chars=4000))
    chunks, banks, spans = generator.analysis_inputs(parsed)
    from arxiv_ra.source_spans import span_batches
    baseline = span_batches(spans, len(chunks))
    assert banks[0] == baseline[0][:len(banks[0])]
    assert sum(len(span['quote']) for span in banks[0]) <= 4000
