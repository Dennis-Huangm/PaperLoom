import copy

import pytest

from arxiv_ra.config import AppConfig
from arxiv_ra.profile_plan import new_draft


def make_draft(basis, *, reference_id='2604.20730', abstract='Render feedback for SVG.'):
    references = [{'arxiv_id': reference_id, 'title': 'SVG agent',
                   'abstract': abstract, 'categories': ['cs.CV']}]
    generated = {
        'conditions': [
            {'kind': 'topic', 'text': 'SVG', 'aliases': ['SVG'],
             'basis': 'input', 'evidence': 'User topic'},
            {'kind': 'prefer', 'text': 'Render feedback', 'aliases': ['render feedback'],
             'basis': basis, 'evidence': 'Reference abstract'},
        ],
        'branches': [{'id': 'core', 'groups': [['SVG']]}],
    }
    original = copy.deepcopy(generated)
    result = new_draft(AppConfig(), 'probe', 'SVG', [], [], references, 'SVG research', generated)
    assert generated == original
    return result


@pytest.mark.parametrize('basis', ['2604.20730', 'arXiv:2604.20730', ' ARXIV: 2604.20730 '])
def test_provided_reference_accepts_arxiv_label(basis):
    draft = make_draft(basis)
    assert draft['status'] == 'ready'
    assert draft['diagnostics'] == []
    assert draft['plan']['conditions'][1]['evidence'].startswith('2604.20730：')
    assert draft['plan']['branches'][0]['groups'] == [['SVG']]


@pytest.mark.parametrize('basis', ['arXiv:9999.12345', 'arXiv:2604.20730v2',
                                  '2604.20730, 9999.12345', ['2604.20730'], None])
def test_unknown_basis_fails_without_secondary_branch_error_or_partial_suggestions(basis):
    draft = make_draft(basis)
    assert draft['status'] == 'failed'
    assert draft['generation_error'] == '模型引用了未提供或未解析的依据'
    assert draft['diagnostics'] == ['模型引用了未提供或未解析的依据']
    assert draft['plan']['conditions'] == []
    assert draft['plan']['branches'] == []


def test_prefix_does_not_allow_missing_reference_content():
    draft = make_draft('arXiv:2604.20730', abstract='')
    assert draft['status'] == 'failed'
    assert draft['generation_error'] == '模型引用了未提供或未解析的依据'


def test_explicit_reference_version_is_not_discarded():
    assert make_draft('arXiv:2604.20730v2', reference_id='2604.20730v2')['status'] == 'ready'
    assert make_draft('arXiv:2604.20730v1', reference_id='2604.20730v2')['status'] == 'failed'


def test_independent_invalid_branch_still_fails():
    generated = {'positive_keywords': ['SVG'], 'branches': [{'id': 'core', 'groups': [['robot']]}]}
    draft = new_draft(AppConfig(), 'probe', 'SVG', ['SVG'], [], [], 'SVG research', generated)
    assert draft['status'] == 'failed'
    assert draft['generation_error'] == '每条分支需要独立的核心主题锚点组'
    assert all(c['origin'] == 'user' for c in draft['plan']['conditions'])
