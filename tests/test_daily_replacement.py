from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from arxiv_ra.config import AppConfig
from arxiv_ra.models import Paper, VerifiedMetadata
from arxiv_ra.pipeline import DailyPipeline
from arxiv_ra.storage import read_recommendations
from arxiv_ra.utils import write_json


def test_same_day_rerun_replaces_and_reconsiders_today(tmp_path, monkeypatch):
    config = AppConfig(output_dir=str(tmp_path), profile_id='test')
    config.discovery.recommendation_count = 2
    config.discovery.minimum_concept_groups = 0
    config.discovery.min_score = -100
    config.ranking.llm_rerank = False
    now = datetime.now(ZoneInfo(config.timezone))
    today = now.date().isoformat()
    def paper(aid):
        return Paper(aid, 'SVG generation', [], 'SVG generation', ['cs.AI'], 'cs.AI',
                     now, now, f'https://arxiv.org/abs/{aid}', f'https://arxiv.org/pdf/{aid}')
    a, b, c, old = [paper(f'2610.0000{i}') for i in range(1, 5)]
    write_json(tmp_path / 'state-test.json', {'processed': [old.arxiv_id]})
    clients = SimpleNamespace(llm=SimpleNamespace(enabled=False),
                              verifier=SimpleNamespace(verify=lambda p: VerifiedMetadata(title=p.title)))
    pipeline = DailyPipeline(config, tmp_path, clients=clients)
    values = [a, b]
    monkeypatch.setattr(pipeline, '_discover_papers', lambda *args, **kwargs: values)
    monkeypatch.setattr(pipeline, '_run_optional_integrations', lambda *args: None)
    monkeypatch.setattr('arxiv_ra.pipeline.localize_abstracts', lambda *args: None)
    pipeline.run(deliver=False)
    assert len(read_recommendations(tmp_path, today, 'test')) == 2
    # Current-day papers remain eligible, earlier days stay excluded at every stage.
    assert pipeline._processed_ids() == {old.arxiv_id}
    values[:] = [a, c, old]
    pipeline.run(deliver=False)
    result = read_recommendations(tmp_path, today, 'test')
    assert {x['paper']['arxiv_id'] for x in result} == {a.arxiv_id, c.arxiv_id}
    assert len(result) == 2
    assert len(list((tmp_path / today / 'batches/test').glob('*/previous.json'))) == 2
    def fail(*args, **kwargs):
        raise ConnectionError('offline')
    monkeypatch.setattr(pipeline, '_discover_papers', fail)
    with pytest.raises(ConnectionError):
        pipeline.run(deliver=False)
    assert read_recommendations(tmp_path, today, 'test') == result
    # A successful empty rerun replaces the list too.
    monkeypatch.setattr(pipeline, '_discover_papers', lambda *args, **kwargs: [])
    pipeline.run(deliver=False)
    assert read_recommendations(tmp_path, today, 'test') == []


def test_today_exemption_is_profile_scoped_and_preserves_older_history(tmp_path):
    config = AppConfig(output_dir=str(tmp_path), profile_id='test')
    pipeline = DailyPipeline(config, tmp_path, clients=SimpleNamespace())
    pipeline._run_date = '2026-10-15'
    write_json(tmp_path / 'state-test.json', {'processed': ['old', 'today', 'other']})
    write_json(tmp_path / '2026-10-14/recommendations-test.json', [{'paper': {'arxiv_id': 'old'}}])
    write_json(tmp_path / '2026-10-15/recommendations-test.json',
               [{'paper': {'arxiv_id': aid}} for aid in ['old', 'today']])
    write_json(tmp_path / '2026-10-15/recommendations-other.json', [{'paper': {'arxiv_id': 'other'}}])
    assert pipeline._processed_ids() == {'old', 'other'}
    pipeline._run_date = '2026-10-16'
    assert pipeline._processed_ids() == {'old', 'today', 'other'}


def test_force_rerun_preserves_history_from_superseded_older_batches(tmp_path):
    config = AppConfig(output_dir=str(tmp_path), profile_id='test')
    pipeline = DailyPipeline(config, tmp_path, clients=SimpleNamespace())
    pipeline._run_date = '2026-10-15'
    write_json(tmp_path / 'state-test.json', {'processed': ['a', 'b', 'today']})
    write_json(tmp_path / '2026-10-14/recommendations-test.json', [{'paper': {'arxiv_id': 'b'}}])
    write_json(tmp_path / '2026-10-14/batches/test/first/recommendations.json',
               [{'paper': {'arxiv_id': 'a'}}])
    write_json(tmp_path / '2026-10-14/batches/other/first/recommendations.json',
               [{'paper': {'arxiv_id': 'today'}}])
    write_json(tmp_path / '2026-10-15/recommendations-test.json',
               [{'paper': {'arxiv_id': aid}} for aid in ['a', 'today']])
    assert pipeline._processed_ids() == {'a', 'b'}
