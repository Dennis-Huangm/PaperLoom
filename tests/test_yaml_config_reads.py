"""Configuration reads remain fresh and independent after parsing is reused."""
import os

import pytest
import yaml

from arxiv_ra.config import load_config
from arxiv_ra.profiles.profiles import ProfileManager


def workspace(tmp_path):
    config = tmp_path / 'config.yaml'
    config.write_text('output_dir: run\n', encoding='utf-8')
    manager = ProfileManager(tmp_path)
    manager.save({'id': 'alpha', 'name': 'Alpha',
                  'discovery': {'positive_keywords': ['original']},
                  'source': {'reference_papers': [{'title': 'Original'}]}})
    manager.active_path.write_text('alpha', encoding='utf-8')
    return config, manager


def test_configuration_reads_do_not_share_mutable_values(tmp_path):
    config, manager = workspace(tmp_path)
    manager.get('alpha')['discovery']['positive_keywords'].append('changed')
    listing = manager.list()
    listing[0]['references'][0]['title'] = 'Changed'
    listing[0]['keywords'].append('changed')
    load_config(config).discovery.positive_keywords.append('changed')
    assert manager.get('alpha')['discovery']['positive_keywords'] == ['original']
    assert manager.list()[0]['references'] == [{'title': 'Original'}]
    assert load_config(config).discovery.positive_keywords == ['original']


def test_same_size_same_timestamp_external_edits_are_immediately_visible(tmp_path):
    config, manager = workspace(tmp_path)
    assert load_config(config).profile_name == 'Alpha'
    assert manager.list()[0]['name'] == 'Alpha'
    profile = manager.root / 'alpha.yaml'
    stat = profile.stat()
    profile.write_text(profile.read_text(encoding='utf-8').replace('Alpha', 'Bravo'), encoding='utf-8')
    os.utime(profile, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    assert profile.stat().st_size == stat.st_size
    assert load_config(config).profile_name == 'Bravo'
    assert manager.get('alpha')['name'] == 'Bravo'
    assert manager.list()[0]['name'] == 'Bravo'
    config_stat = config.stat()
    config.write_text('output_dir: new\n', encoding='utf-8')
    os.utime(config, ns=(config_stat.st_atime_ns, config_stat.st_mtime_ns))
    assert load_config(config).output_dir == 'new'


def test_profile_switch_and_deletion_never_reuse_old_identity(tmp_path):
    config, manager = workspace(tmp_path)
    assert load_config(config).profile_id == 'alpha'
    manager.save({'id': 'beta', 'name': 'Beta'})
    manager.active_path.write_text('beta', encoding='utf-8')
    assert load_config(config).profile_id == 'beta'
    assert manager.list()[0]['id'] == 'beta'
    (manager.root / 'beta.yaml').unlink()
    assert load_config(config).profile_id == ''
    with pytest.raises(KeyError):
        manager.get('beta')
    assert [p['id'] for p in manager.list()] == ['alpha']


def test_configuration_still_rejects_python_yaml_tags(tmp_path):
    config, _ = workspace(tmp_path)
    config.write_text('!!python/object/apply:builtins.str [unsafe]\n', encoding='utf-8')
    with pytest.raises(yaml.constructor.ConstructorError):
        load_config(config)
