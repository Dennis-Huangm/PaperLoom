from arxiv_ra.evidence import attach_evidence
from arxiv_ra.models import ParsedPaper


def test_missing_table_citation_is_diagnostic_not_cell_warning():
    report = ('# Paper\n\n## 关键结果\n\n### Table 3（部分摘录）\n\n'
              '| Model | Full | 原文依据 |\n|---|---|---|\n'
              '| Claude Sonnet 5 | 15.0 | [[证据ID:missing]] |')
    output, evidence = attach_evidence(report, ParsedPaper('', ['Table 3: Results']),
                                      pdf_available=True, full_report=True)
    assert '| Claude Sonnet 5 | 15.0 |' in output
    assert '待核对' not in output
    assert 'numeric_audit' not in evidence


def test_uncited_table_and_prose_are_published_without_review():
    report = ('## 关键结果\n\n准确率为 99.9%。\n\n'
              '| Model | Score |\n|---|---|\n| Alpha 5 | 91.2 |')
    output, evidence = attach_evidence(report, ParsedPaper('', ['Source text.']),
                                      pdf_available=True, full_report=True)
    assert '| Alpha 5 | 91.2 |' in output
    assert '准确率为 99.9%' in output and '**[待核对]**' not in output
    assert 'numeric_audit' not in evidence


def test_pipeline_does_not_report_component_failure_for_uncited_table(tmp_path):
    from pathlib import Path
    from types import SimpleNamespace
    from arxiv_ra.config import AppConfig
    from arxiv_ra.models import Paper, VerifiedMetadata
    from arxiv_ra.pipeline import DailyPipeline
    from arxiv_ra.task_runtime import TaskHooks, bind_task_hooks

    config = AppConfig(output_dir=str(tmp_path / 'run'), profile_id='test')
    config.obsidian.enabled = False
    report = '# Paper\n\n## 关键结果\n\n| Model | Score |\n|---|---|\n| Alpha 5 | 91.2 |'
    clients = SimpleNamespace(
        llm=SimpleNamespace(enabled=True),
        reporter=SimpleNamespace(generate=lambda *a: report),
        verifier=SimpleNamespace(verify=lambda p: VerifiedMetadata(title=p.title)),
        parser=SimpleNamespace(parse=lambda *a: ParsedPaper('Source text', ['Source text'])),
        arxiv_html=SimpleNamespace(fetch=lambda *a: []),
    )
    pdf = tmp_path / 'source.pdf'
    pdf.write_bytes(b'Parser is mocked')
    warnings = []
    hooks = TaskHooks(lambda *a: None, lambda *a: warnings.append(a), lambda: False)
    with bind_task_hooks(hooks):
        artifact = DailyPipeline(config, tmp_path, clients=clients)._process_paper(
            Paper.from_dict({'arxiv_id': '2501.00001', 'title': 'Paper'}),
            Path(config.output_dir) / '2026-09-30', False, local_pdf=pdf)
    assert not any(component == '报告数值核对' for component, _ in warnings)
    assert '| Alpha 5 | 91.2 |' in artifact.report_path.read_text(encoding='utf-8')
