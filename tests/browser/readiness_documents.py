"""Real page renderers for isolated browser readiness checks (no user data)."""
import json
from pathlib import Path
from types import SimpleNamespace
from jinja2 import Environment, FileSystemLoader
from arxiv_ra.render import report_document
from arxiv_ra.task_result import task_document

env = Environment(loader=FileSystemLoader(Path('src/arxiv_ra/templates')), autoescape=True)
reading = env.get_template('reading.html').render(
    config=SimpleNamespace(profile_id='fixture', profile_name='Fixture'), active='reading',
    url_for=lambda name, path: '/static' + path,
    arxiv_id='', report_id='', origin='', source_date='',
)
print(json.dumps({
    'reading': reading,
    'report': report_document('# Fixture\n\n## Formula\n\n$x^2$\n\n' + '\n\n'.join('$$x_%d^2$$' % i for i in range(80)), 'Fixture', '2609.29718v2', report_id='fixture'),
    'task': task_document('# Fixture\n\n$x^2$', 'Fixture', 'tracking'),
    'offline': report_document('# Fixture\n\n$x^2$', 'Fixture', asset_base=Path('src/arxiv_ra/static').resolve().as_uri()),
    'offline_paper': report_document('# Fixture\n\n$x^2$', 'Fixture', '2609.29718v2', asset_base=Path('src/arxiv_ra/static').resolve().as_uri()),
}))
