from copy import deepcopy
from types import SimpleNamespace

import pytest

from arxiv_ra.config import AppConfig
from arxiv_ra.profile_plan import new_draft
from arxiv_ra.profile_preview import preview_profile
from arxiv_ra.research_clients import ResearchClients


def test_preview_has_independent_visible_date_range(monkeypatch):
    cfg = AppConfig()
    draft = new_draft(cfg, 'svg', 'SVG', ['SVG'], [], [], 'SVG research', {})
    draft['discovery']['lookback_days'] = 2
    before = deepcopy(draft)
    calls = []
    def search(categories, days, limit, **kwargs):
        calls.append(days)
        return []
    monkeypatch.setattr(ResearchClients, 'arxiv', property(lambda _: SimpleNamespace(search=search)))
    monkeypatch.setattr(ResearchClients, 'alphaxiv', property(lambda _: SimpleNamespace(enabled=False)))
    monkeypatch.setattr(ResearchClients, 'llm', property(lambda _: SimpleNamespace(enabled=False)))
    result = preview_profile(draft, cfg)
    assert calls == [90]
    assert result['scope']['lookback_days'] == 90
    assert result['scope']['daily_lookback_days'] == 2
    assert result['scope']['date_from'] < result['scope']['date_to']
    assert draft == before
    calls.clear()
    result = preview_profile(draft, cfg, lookback_days=2)
    assert calls == [2]
    assert result['scope']['lookback_days'] == 2


@pytest.mark.parametrize('days', [0, 366, True, '90'])
def test_preview_rejects_invalid_range(days):
    with pytest.raises(ValueError):
        preview_profile({}, AppConfig(), lookback_days=days)
