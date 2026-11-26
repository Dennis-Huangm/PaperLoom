from pathlib import Path
from types import SimpleNamespace

import pytest

from paperloom.config import AppConfig
from paperloom.papers.models import FigureCandidate, VerifiedMetadata
from paperloom.reports.report_figures import experiment_block
from paperloom.reports.report_revision import revise_report
from paperloom.utils import read_json, write_json


@pytest.fixture
def saved(tmp_path):
    folder = tmp_path / '2026-11-01' / 'reports' / 'source'
    folder.mkdir(parents=True)
    paper = dict(arxiv_id='1312.6114', version=11, metadata_status='complete', title='Paper',
                 authors=[{'name': 'Author'}])
    write_json(folder / 'metadata.json', dict(paper=paper, verified=VerifiedMetadata(title='Paper').to_dict(),
                                             profile_id='original', report_quality='full'))
    (folder / 'report.html').write_text('original')
    figures = [FigureCandidate(folder / f'{n}.png', n, f'Figure {n}: source', role='experiment',
                               explanation=f'旧解读{n}', image_available=False) for n in (2, 3)]
    rows = [dict(page=f.page, caption=f.caption, explanation=f.explanation, filename=None) for f in figures]
    write_json(folder / 'experiment-figures.json', rows)
    report = ('# Paper\n\n## 关键结果\n\n' + '\n\n'.join(experiment_block(f, f.page) for f in figures)
              + '\n\n<!-- experiment-finding:2,3 -->\n\n旧结论\n\n<!-- /experiment-finding -->\n\n无关段落。')
    (folder / 'report.md').write_text(report, encoding='utf-8')
    return folder


def revise(folder, stage, clients=None, **options):
    root = folder.parents[2]
    return revise_report(root, AppConfig(), clients, '1312.6114v11',
                         (folder/'report.html').relative_to(root).as_posix(), stage,
                         '012345abcdef', '2026-11-02', **options)


def test_render_and_retry_do_not_access_clients_or_change_markdown(saved):
    path = revise(saved, 'render')
    assert path.read_bytes() == (saved/'report.md').read_bytes()
    assert path == revise(saved, 'render')
    assert 'revision-012345abcdef' in path.with_suffix('.html').read_text(encoding='utf-8')
    assert (saved/'report.html').read_text() == 'original'
    assert read_json(path.with_name('metadata.json'))['profile_id'] == 'original'


def test_figure_updates_one_reading_and_reviews_its_shared_finding(saved):
    calls = []
    def explain(n, paper, fig, parsed):
        calls.append(n)
        fig.explanation = '新的中文解读'
        fig.explanation_basis = 'text'
    def review(system, prompt):
        calls.append('cross')
        assert '旧解读2' in prompt and '新的中文解读' in prompt
        return 'REVIEW'
    clients = SimpleNamespace(reporter=SimpleNamespace(explain_figure=explain), llm=SimpleNamespace(chat=review))
    path = revise(saved, 'figure', clients, figure=3)
    result = path.read_text(encoding='utf-8')
    assert calls == [3, 'cross']
    assert '新的中文解读' in result and '旧解读2' in result and '旧解读3' not in result
    assert '跨图结论待复核' in result and '旧结论' not in result and result.endswith('无关段落。')
    assert read_json(path.with_name('experiment-figures.json'))[0] == read_json(saved/'experiment-figures.json')[0]


def test_failed_scoped_revision_does_not_publish_or_modify_source(saved):
    with pytest.raises(ValueError, match='实验图 8'):
        revise(saved, 'figure', object(), figure=8)
    assert list(saved.parents[2].glob('*/reports/*/metadata.json')) == [saved/'metadata.json']


def test_republish_uses_no_model_and_retains_source_page_links(saved):
    from paperloom.reports.tables.report_tables import ReportTables
    (saved/'report-draft.md').write_text('# Paper\n\n## 关键结果\n\n[[实验图:figure-3]]', encoding='utf-8')
    write_json(saved/'tables.json', ReportTables(()).to_dict())
    text = revise(saved, 'publish').read_text(encoding='utf-8')
    assert text.count('旧解读3') == 1 and '[查看原文第 3 页](https://arxiv.org/pdf/1312.6114v11#page=3)' in text


def test_cancel_at_revision_commit_preserves_only_original(saved):
    from unittest.mock import Mock
    from paperloom.runtime.jobs.task_runtime import TaskCancelled, TaskHooks, bind_task_hooks

    hooks = TaskHooks(lambda *_: None, lambda *_: None, lambda: False, commit=Mock(side_effect=TaskCancelled))
    with bind_task_hooks(hooks), pytest.raises(TaskCancelled):
        revise(saved, 'render')
    assert list(saved.parents[2].glob('*/reports/*/metadata.json')) == [saved/'metadata.json']


def test_checked_reading_is_bound_to_image_caption_and_paper(saved):
    from paperloom.papers.models import Paper
    from paperloom.reports.report_checkpoint import fingerprint
    from paperloom.reports.reviewed_material import apply_figure_review, figure_review_signature, reviewed_material
    root = saved.parents[2]
    paper = Paper.from_dict(read_json(saved/'metadata.json')['paper'])
    img = saved/'plot.png'
    img.write_bytes(b'checked bytes')
    fig = FigureCandidate(img, 6, 'Figure 3: Test', role='experiment')
    reading = {'explanation': '已核对解读', 'signature': figure_review_signature(fig, '已核对解读')}
    path = root/'metadata-cache'/'reviews'/(fingerprint([paper.arxiv_id, paper.version])+'.json')
    write_json(path, dict(arxiv_id=paper.arxiv_id, version=paper.version, title=paper.title,
                          authors=['Author'], figures={'3': reading}))
    record = reviewed_material(root, paper)
    apply_figure_review(fig, record['figures'])
    assert fig.explanation == reading['explanation'] and fig.review_signature == reading['signature']
    paper.version += 1
    assert not reviewed_material(root, paper)
    img.write_bytes(b'changed')
    new = FigureCandidate(img, 6, fig.caption, role='experiment')
    apply_figure_review(new, record['figures'])
    assert not new.review_signature and not new.explanation


def test_metadata_update_marks_only_obsolete_body_claim(saved):
    d = read_json(saved/'metadata.json')
    d['verified']['doi'] = '10.1234/old'
    write_json(saved/'metadata.json', d)
    with (saved/'report.md').open('a', encoding='utf-8') as stream:
        stream.write('\n\n引用本研究的 DOI 为 10.1234/old。')
    v = VerifiedMetadata(title='Paper', doi='10.1234/new', sources=['Crossref'])
    path = revise(saved, 'metadata', SimpleNamespace(verifier=SimpleNamespace(verify=lambda p: v)))
    result = path.read_text(encoding='utf-8')
    assert r'10\.1234/new' in result and result.count('下段沿用旧记录') == 1 and '无关段落。' in result


def test_local_publication_review_preserves_more_precise_verified_fields():
    from paperloom.reports.reviewed_material import apply_publication_review
    current = VerifiedMetadata(venue='Conference', publication_date='2025-10-27', doi='10.1234/example')
    assert not apply_publication_review(current, {'venues': [], 'sources': []})
    assert apply_publication_review(current, {'venues': ['Conference'], 'sources': ['proceedings'], 'date': '2025'})
    assert (current.doi, current.publication_date) == ('10.1234/example', '2025-10-27')


def test_legacy_english_method_caption_is_collapsed_on_republication():
    from paperloom.reports.report import finalize_report_structure
    cap = 'Figure 1: Original explanation in English.'
    fig = FigureCandidate(Path('method.png'), 1, cap, explanation=cap)
    output = finalize_report_structure('# Paper\n\n## 核心方法\n正文。', [fig])
    assert output.count(cap) == 1
    assert '中文解读暂不可用。' in output and f'<p>{cap}</p></details>' in output
