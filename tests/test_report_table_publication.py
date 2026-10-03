"""Check preservation at the actual report publication boundary."""
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import Mock

import pytest

from arxiv_ra.config import AppConfig
from arxiv_ra.models import Paper, ParsedPaper, VerifiedMetadata
from arxiv_ra.pipeline import DailyPipeline
from arxiv_ra.report_tables import GeneratedReport, ReportTables
from arxiv_ra.utils import read_json


@pytest.mark.parametrize('stage', ['normal', 'outline', 'evidence'])
def test_pipeline_publishes_frozen_tables_or_preserves_them_after_failed_review(tmp_path, monkeypatch, stage):
    config = AppConfig(output_dir=str(tmp_path / 'run'), profile_id='alpha')
    config.obsidian.enabled = False
    note = '### Table 1: Scores\n| Model | Score |\n|---|---|\n| A | 91.2 |'
    catalogue = ReportTables.from_notes([note], [{'number':1}])
    draft = '# Paper\n\n## 关键结果\n\n[[表格:table-1]]'
    generated = GeneratedReport(catalogue.render(draft), catalogue, draft)
    clients = SimpleNamespace(
        llm=SimpleNamespace(enabled=True),
        reporter=SimpleNamespace(generate=Mock(return_value=generated)),
        verifier=SimpleNamespace(verify=Mock(return_value=VerifiedMetadata(title='Paper'))),
        arxiv=SimpleNamespace(download_pdf=lambda paper, target: target.write_bytes(b'fixture PDF')),
        parser=SimpleNamespace(parse=lambda *args: ParsedPaper('Sparse source', ['Sparse source'])),
        arxiv_html=SimpleNamespace(fetch=lambda *args: []))
    if stage == 'outline':
        import arxiv_ra.pipeline as module
        finalize = module.finalize_report_structure
        monkeypatch.setattr(module, 'finalize_report_structure',
                            lambda *args: finalize(*args).replace('91.2', '9.12'))
    if stage == 'evidence':
        import arxiv_ra.pipeline as module
        attach = module.attach_evidence
        def damage_evidence(*args, **kwargs):
            report, evidence = attach(*args, **kwargs)
            return report.replace('91.2', '9.12'), evidence
        monkeypatch.setattr(module, 'attach_evidence', damage_evidence)
    damage = stage == 'evidence'
    pipeline = DailyPipeline(config, tmp_path, clients=clients)
    paper = Paper.from_dict({'arxiv_id':'2502.19453', 'title':'Paper', 'version':1})
    if damage:
        with pytest.raises(RuntimeError, match='内容保全'):
            pipeline._process_paper(paper, pipeline.output_root / '2026-10-02', False)
    else:
        artifact = pipeline._process_paper(paper, pipeline.output_root / '2026-10-02', False)
        assert '91.2' in artifact.report_path.read_text(encoding='utf-8')
        assert artifact.report_path.with_suffix('.html').is_file()
    folder = next(Path(config.output_dir).glob('*/reports/*'))
    assert ReportTables.from_dict(read_json(folder / 'tables.json')) == catalogue
    assert (folder / 'report-draft.md').read_text(encoding='utf-8') == draft
    assert read_json(folder / 'table-preservation.json')['status'] == ('failed' if damage else 'passed')
    if damage:
        assert not (folder / 'report.md').exists()
