"""Durable paper collection receipts shared by ordinary and version exports."""
from __future__ import annotations

import copy
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import json
import re
import uuid
from pathlib import Path
from urllib.parse import urlencode

from .obsidian import ObsidianExporter
from .paper_data import base_id, requested_version
from .reading_state import _locked
from .utils import read_json, write_json
from .zotero import ZoteroClient, ZoteroConflict


_ACTIVE_OPERATIONS: set[str] = set()


def zotero_result(operation):
    step = operation['targets']['zotero']
    return {'created': step.get('created', False), 'attachments_added': step.get('attachments_added', 0),
            'item_key': step.get('item_key', ''), 'operation_id': operation['operation_id'],
            'status': step['status'], 'error': step.get('error', ''),
            'receipt_url': '/collection?' + urlencode({'arxiv_id': operation['request']['paper']['arxiv_id']})}


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def _now():
    return datetime.now(timezone.utc).isoformat()


class PaperCollection:
    def __init__(self, config, project_root, *, clients=None):
        self.config = copy.deepcopy(config)
        self.project_root = Path(project_root).resolve()
        output = Path(config.output_dir)
        self.output = (output if output.is_absolute() else self.project_root / output).resolve()
        self.path = self.output / 'collaboration.json'
        self.lock = self.output / 'collaboration.lock'
        self.clients = clients

    def _read(self):
        data = read_json(self.path, {}) or {}
        data.setdefault('operations', {})
        data.setdefault('associations', {})
        restored = read_json(self.project_root / '.paperloom-restore.json', {}) or {}
        generation = restored.get('generation') or restored.get('archive_sha256', '')
        if (data.get('storage_root', str(self.output)) != str(self.output)
                or generation != data.get('restore_generation', '')):
            data['restored'] = True
            data['checked_after_restore'] = []
            for entry in data['associations'].values():
                entry['status'] = 'unverified'
        data['storage_root'] = str(self.output)
        data['restore_generation'] = generation
        return data

    def _restore_pending(self, data, aid):
        return data.get('restored') and f'{self.config.profile_id or "default"}:{aid}' not in data.get('checked_after_restore', [])

    def _settings(self):
        return {name: asdict(getattr(self.config, name)) for name in ('zotero', 'obsidian')}

    def _legacy_receipts(self, aid):
        if not re.fullmatch(r'(?:[a-z-]+(?:\.[a-z]{2})?/\d{7}|\d{4}\.\d{4,5})', aid, re.I):
            return []
        folder = self.output / 'papers' / (self.config.profile_id or 'default') / aid.replace('/', '-')
        state = read_json(folder / 'sync.json', {}) or {}
        result = []
        for revision, old in state.get('operations', {}).items():
            if not str(revision).isdigit():
                continue
            targets = {name: {**step, 'status': 'unverified'} for name, step in old.get('steps', {}).items()
                       if name in {'zotero', 'obsidian'} and not step.get('operation_id')}
            if not targets:
                continue
            result.append({'operation_id': 'legacy-' + str(revision), 'legacy': True, 'status': 'unverified',
                'created_at': old.get('updated_at', ''), 'targets': targets,
                'request': {'paper': {'arxiv_id': aid, 'version': int(revision), 'title': state.get('title', aid)},
                            'targets': list(targets), 'material_hashes': {}}})
        return result

    def collect(self, item, *, zotero=False, obsidian=False, report_path=None,
                pdf_path=None, collection_key='', pdf_loader=None, zotero_factory=None):
        paper = copy.deepcopy(item['paper'])
        paper['arxiv_id'] = base_id(paper['arxiv_id'])
        if not zotero and not obsidian:
            raise ValueError('请至少选择一个收录目标')
        request = {'paper': paper, 'verified': item.get('verified') or {},
                   'profile_id': self.config.profile_id or 'default',
                   'profile_name': self.config.profile_name, 'settings': self._settings(),
                   'targets': [name for name, enabled in [('zotero', zotero), ('obsidian', obsidian)] if enabled],
                   'collection_key': collection_key,
                   'report_path': str(Path(report_path).resolve()) if report_path else '',
                   'report_html': str(Path(report_path).with_suffix('.html').resolve()) if report_path else '',
                   'pdf_path': str(Path(pdf_path).resolve()) if pdf_path else ''}
        request['material_hashes'] = {key: hashlib.sha256(Path(request[key]).read_bytes()).hexdigest()
                                      for key in ('report_path', 'pdf_path') if request[key]}
        if zotero and self.config.zotero.attach_report and request['report_html']:
            html = Path(request['report_html'])
            request['material_hashes']['report_html'] = hashlib.sha256(html.read_bytes()).hexdigest() if html.is_file() else ''
        operation_id = _digest(request)
        with _locked(self.lock):
            data = self._read()
            operation = data['operations'].setdefault(operation_id, {
                'operation_id': operation_id, 'request': request, 'targets': {}, 'created_at': _now()})
            return self._execute(data, operation, pdf_loader=pdf_loader, zotero_factory=zotero_factory)

    def _execute(self, data, operation, *, pdf_loader=None, zotero_factory=None):
        identity = str(self.path) + operation['operation_id']
        _ACTIVE_OPERATIONS.add(identity)
        try:
            return self._run(data, operation, pdf_loader=pdf_loader, zotero_factory=zotero_factory)
        finally:
            _ACTIVE_OPERATIONS.discard(identity)

    def _run(self, data, operation, *, pdf_loader=None, zotero_factory=None):
        request = operation['request']
        self.config.profile_name = request['profile_name']
        for target in request['targets']:
            step = operation['targets'].setdefault(target, {})
            if self._restore_pending(data, request['paper']['arxiv_id']):
                step.update(status='needs_attention', error='回执来自恢复目录，请先检查外部关联')
                continue
            try:
                self._validate_materials(request, target)
            except ValueError as exc:
                step.update(status='needs_attention', error=str(exc), updated_at=_now())
                continue
            if target == 'obsidian':
                association = self._check_note(data, request['paper'])
                if association.get('status') == 'needs_attention':
                    step.update(status='needs_attention', error=association['error'], candidates=association.get('candidates', []))
                    continue
                if step.get('status') == 'succeeded' and association.get('path'):
                    step['path'] = association['path']
                    continue
            step.update(status='running', error='', updated_at=_now())
            write_json(self.path, data)
            try:
                if target == 'obsidian':
                    self._obsidian(data, request, step, pdf_loader)
                else:
                    self._zotero(data, request, step, pdf_loader, zotero_factory)
                step.update(status='succeeded', updated_at=_now())
            except ZoteroConflict as exc:
                step.update(status='needs_attention', error=str(exc), candidates=exc.candidates)
            except Exception as exc:
                step.update(status='partial' if step.get('item_key') else 'failed',
                            error=f'{type(exc).__name__}: {exc}', updated_at=_now())
            write_json(self.path, data)
        self._link_note(data, operation)
        statuses = {step.get('status') for step in operation['targets'].values()}
        operation['status'] = ('succeeded' if statuses == {'succeeded'} else 'partial'
                               if 'succeeded' in statuses or 'partial' in statuses else 'needs_attention'
                               if 'needs_attention' in statuses else 'failed')
        operation['updated_at'] = _now()
        write_json(self.path, data)
        return copy.deepcopy(operation)

    def retry(self, operation_id, *, pdf_loader=None, selected_key='', use_original=False, zotero_factory=None):
        with _locked(self.lock):
            data = self._read()
            operation = data['operations'].get(operation_id)
            if not operation or operation['request']['profile_id'] != (self.config.profile_id or 'default'):
                raise ValueError('未找到本研究方向的收录记录')
            request = operation['request']
            if request['settings'] != self._settings():
                if not use_original:
                    raise ValueError('目标配置已变化；请明确按原配置重试，或从论文发起新收录')
                for name, settings in request['settings'].items():
                    setattr(self.config, name, type(getattr(self.config, name))(**settings))
            if selected_key:
                step = operation['targets'].get('zotero', {})
                if selected_key not in {item['key'] for item in step.get('candidates', [])}:
                    raise ValueError('请从本次匹配候选中选择条目')
                step.update(item_key=selected_key, status='pending', candidates=[], files={})
            return self._execute(data, operation, pdf_loader=pdf_loader, zotero_factory=zotero_factory)

    def recollect(self, operation_id, target, *, pdf_loader=None):
        if target not in {'zotero', 'obsidian'}:
            raise ValueError('请选择收录目标')
        with _locked(self.lock):
            data = self._read()
            old = data['operations'].get(operation_id)
            if not old or old['request']['profile_id'] != (self.config.profile_id or 'default'):
                raise ValueError('未找到本研究方向的收录记录')
            request = copy.deepcopy(old['request'])
            self._validate_materials(request, target)
            paper = request['paper']
            if target == 'obsidian':
                with ObsidianExporter(self.config, self.project_root, clients=self.clients) as exporter:
                    exporter.forget_missing_paper_note(paper['arxiv_id'])
                data['associations'].pop(self._obsidian_key(paper), None)
            else:
                client = ZoteroClient(self.config.zotero)
                try:
                    identity = client.library_identity()
                    data['associations'].pop(_digest(['zotero', identity, paper['arxiv_id']]), None)
                finally:
                    client.client.close()
            request.update(settings=self._settings(), targets=[target], recollects=operation_id,
                           nonce=uuid.uuid4().hex)
            new_id = _digest(request)
            operation = {'operation_id': new_id, 'request': request, 'targets': {}, 'created_at': _now()}
            data['operations'][new_id] = operation
            return self._execute(data, operation, pdf_loader=pdf_loader)

    @staticmethod
    def _validate_materials(request, target):
        for key, digest in request['material_hashes'].items():
            if key == 'report_html' and target != 'zotero':
                continue
            path = Path(request[key])
            if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != digest:
                raise ValueError('原收录材料已变化或缺失，请从论文发起新收录')

    def _pdf(self, data, request, step, pdf_loader):
        if request['pdf_path']:
            return Path(request['pdf_path'])
        previous = step.get('downloaded_pdf')
        if previous:
            path = Path(previous['path'])
            if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != previous['sha256']:
                raise ZoteroConflict('原收录 PDF 已变化或缺失，请从论文发起新收录')
            return path
        if not request['paper'].get('version'):
            raise ZoteroConflict('论文版本未知，请确认指定版本后收录 PDF')
        path = pdf_loader(request['paper'], True) if pdf_loader else None
        if not path or not path.is_file():
            raise ValueError('所选 PDF 材料尚不可用')
        step['downloaded_pdf'] = {'path': str(path.resolve()), 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}
        write_json(self.path, data)
        return path

    def _zotero(self, data, request, step, pdf_loader, factory):
        if not self.config.zotero.enabled:
            raise ValueError('Zotero 尚未启用，请前往设置')
        client = (factory or ZoteroClient)(self.config.zotero)
        try:
            identity = client.library_identity()
            if step.get('library_id') and step['library_id'] != identity:
                raise ZoteroConflict('Zotero 文献库已变化，请按新目标重新收录')
            step['library_id'] = identity
            data.setdefault('zotero_libraries', {})[self.config.zotero.base_url] = identity
            key = _digest(['zotero', identity, request['paper']['arxiv_id']])
            association = data['associations'].get(key, {})
            if step.get('item_key') and association.get('replaces') == step['item_key']:
                step.update(item_key=association['item_key'], files={})
            if not step.get('item_key') and association.get('item_key'):
                step['item_key'] = association['item_key']
            if step.get('item_key') == association.get('item_key'):
                step['files'] = {**association.get('files', {}), **step.get('files', {})}
            def checkpoint():
                if step.get('item_key'):
                    value = {**data['associations'].get(key, {}), 'target': 'zotero', 'library_id': identity,
                        'item_key': step['item_key'], 'arxiv_id': request['paper']['arxiv_id'],
                        'files': copy.deepcopy(step.get('files', {})),
                        'base_url': self.config.zotero.base_url, 'status': 'verified', 'verified_at': _now()}
                    if step.get('version'):
                        value.update(version=step['version'], report_path=step['report_path'],
                                     material_hashes=request['material_hashes'])
                    data['associations'][key] = value
                write_json(self.path, data)
            pdf = self._pdf(data, request, step, pdf_loader) if self.config.zotero.attach_pdf else None
            result = client.save_paper(request['paper'], request['verified'], request['profile_name'],
                report_path=Path(request['report_path']).with_suffix('.html') if request['report_path'] else None,
                pdf_path=pdf, collection_key=request['collection_key'] or None,
                receipt=step, checkpoint=checkpoint)
            step.update(result.to_dict(), version=request['paper'].get('version'), report_path=request['report_path'])
            checkpoint()
        finally:
            client.client.close()

    def _link_note(self, data, operation):
        request = operation['request']
        zotero = operation['targets'].get('zotero', {})
        note = data['associations'].get(self._obsidian_key(request['paper']), {})
        if not self.config.obsidian.enabled or zotero.get('status') in {'needs_attention', 'failed'}:
            return
        if not zotero.get('item_key'):
            identity = data.get('zotero_libraries', {}).get(self.config.zotero.base_url)
            zotero = data['associations'].get(_digest(['zotero', identity, request['paper']['arxiv_id']]), {})
            if zotero.get('status') != 'verified':
                return
        if not zotero.get('item_key') or not note.get('path') or self._restore_pending(data, request['paper']['arxiv_id']):
            return
        step = operation['targets'].setdefault('obsidian_link', {})
        try:
            with ObsidianExporter(self.config, self.project_root, clients=self.clients) as exporter:
                exporter.link_zotero(note['path'], request['paper']['arxiv_id'], zotero['item_key'])
            step.update(status='succeeded', path=note['path'])
        except Exception as exc:
            step.update(status='failed', error=str(exc))

    def _obsidian_key(self, paper):
        settings = self.config.obsidian
        return _digest(['obsidian', str(Path(settings.vault_path).resolve()), settings.root_folder,
                        self.config.profile_id or 'default', base_id(paper['arxiv_id'])])

    def _check_note(self, data, paper, selected_path=''):
        key = self._obsidian_key(paper)
        previous = data['associations'].get(key, {})
        if not self.config.obsidian.enabled:
            return {'status': 'unconfigured', 'error': 'Obsidian 尚未启用'}
        try:
            with ObsidianExporter(self.config, self.project_root, clients=self.clients) as exporter:
                candidates = exporter.find_paper_notes(paper['arxiv_id'])
                historical = exporter.historical_paper_note(paper['arxiv_id'])
                preferred = selected_path or previous.get('chosen_path', '')
                chosen = next((entry for entry in candidates if entry['path'] == preferred), None) if preferred else (
                    candidates[0] if len(candidates) == 1 else None)
                if chosen and chosen['valid']:
                    exporter.associate_paper_note(paper['arxiv_id'], chosen['path'])
                    value = {**previous, 'target': 'obsidian', 'arxiv_id': paper['arxiv_id'],
                             'status': 'verified', 'verified_at': _now(), **chosen}
                    if selected_path:
                        value['chosen_path'] = selected_path
                elif candidates or previous.get('path') or historical:
                    value = {**previous, 'status': 'needs_attention', 'candidates': candidates,
                             'error': '笔记缺失、托管标记损坏或存在多个匹配，请检查关联后选择或明确重新收录'}
                else:
                    value = {'status': 'unlinked'}
        except Exception as exc:
            value = {**previous, 'status': 'needs_attention', 'error': str(exc)}
        data['associations'][key] = value
        return value

    def check(self, arxiv_id, *, selected_path='', selected_key=''):
        aid = base_id(arxiv_id)
        with _locked(self.lock):
            data = self._read()
            associations = {'obsidian': self._check_note(data, {'arxiv_id': aid}, selected_path)}
            if self.config.zotero.enabled:
                associations['zotero'] = self._check_zotero(data, aid, selected_key)
            write_json(self.path, data)
            relevant = [data['associations'].get(self._obsidian_key({'arxiv_id': aid}), {})]
            library_id = data.get('zotero_libraries', {}).get(self.config.zotero.base_url)
            relevant.append(data['associations'].get(_digest(['zotero', library_id, aid]), {}))
            if (all(value.get('status') != 'needs_attention' for value in associations.values())
                    and all(value.get('status') == 'verified' for value in relevant
                            if value.get('path') or value.get('item_key'))):
                checked = data.setdefault('checked_after_restore', [])
                identity = f'{self.config.profile_id or "default"}:{aid}'
                if identity not in checked:
                    checked.append(identity)
                write_json(self.path, data)
            return {'associations': associations}

    def _check_zotero(self, data, aid, selected_key):
        client = ZoteroClient(self.config.zotero)
        key = None
        previous = {}
        try:
            identity = client.library_identity()
            data.setdefault('zotero_libraries', {})[self.config.zotero.base_url] = identity
            key = _digest(['zotero', identity, aid])
            previous = data['associations'].get(key, {})
            item_key = previous.get('item_key')
            original_key = item_key
            if selected_key:
                if selected_key not in {item['key'] for item in previous.get('candidates', [])}:
                    raise ValueError('请从检查结果中选择条目')
                item_key = selected_key
            if item_key and client.get_item(item_key) is None:
                item_key = None
            if not item_key:
                item_key = client.find_paper({'arxiv_id': aid}, {})
                if original_key and item_key and not selected_key:
                    candidate = client.get_item(item_key)
                    raise ZoteroConflict('原关联条目已删除，请确认要改绑的文献',
                                         [{'key': item_key, 'title': candidate.get('title', item_key)}])
            if item_key:
                previous = {**previous, 'target': 'zotero', 'status': 'verified', 'verified_at': _now(),
                    'item_key': item_key, 'library_id': identity, 'arxiv_id': aid,
                    'base_url': self.config.zotero.base_url, 'candidates': [], 'error': ''}
                if selected_key and original_key != selected_key:
                    previous.update(replaces=original_key, files={})
            else:
                previous = {**previous, 'target': 'zotero', 'library_id': identity, 'arxiv_id': aid,
                    'status': 'needs_attention' if previous.get('item_key') else 'unlinked',
                    'error': '关联条目已删除，请明确重新收录' if previous.get('item_key') else ''}
        except ZoteroConflict as exc:
            previous = {**previous, 'target': 'zotero', 'arxiv_id': aid, 'library_id': identity,
                        'status': 'needs_attention', 'error': str(exc), 'candidates': exc.candidates}
        except Exception as exc:
            previous = {**previous, 'status': 'needs_attention', 'error': str(exc)}
        finally:
            client.client.close()
        if key:
            data['associations'][key] = previous
        return previous

    def _obsidian(self, data, request, step, pdf_loader=None):
        with ObsidianExporter(self.config, self.project_root, clients=self.clients) as exporter:
            report = request['report_path']
            pdf = self._pdf(data, request, step, pdf_loader) if self.config.obsidian.copy_pdf else None
            path = exporter.sync_collection(request['paper'], request['verified'],
                Path(report).with_suffix('.md') if report else None, pdf)
            relative = path.resolve().relative_to(Path(self.config.obsidian.vault_path).resolve()).as_posix()
            step.update(path=relative, version=request['paper'].get('version'), report_path=report)
            key = self._obsidian_key(request['paper'])
            data['associations'][key] = {**data['associations'].get(key, {}),
                'target': 'obsidian', 'arxiv_id': request['paper']['arxiv_id'], 'status': 'verified', 'path': relative,
                'version': request['paper'].get('version'), 'report_path': report,
                'material_hashes': request['material_hashes']}

    def status(self, arxiv_id):
        aid = base_id(arxiv_id)
        data = self._read()
        return self._status(data, arxiv_id, aid)

    def page_links(self):
        """Build links from one receipt snapshot for a rendered page."""
        data = self._read()
        links = {}
        identity = data.get('zotero_libraries', {}).get(self.config.zotero.base_url)
        for association in data['associations'].values():
            if (association.get('target') == 'zotero' and association.get('library_id') == identity
                    and association.get('status') == 'verified'):
                aid = association['arxiv_id']
                if not self._restore_pending(data, aid):
                    links.setdefault(aid, {})['zotero'] = 'zotero://select/library/items/' + association['item_key']
        vault = Path(self.config.obsidian.vault_path).resolve()
        for key, association in data['associations'].items():
            aid = association.get('arxiv_id')
            if (not aid or key != self._obsidian_key({'arxiv_id': aid}) or not association.get('path')
                    or association.get('status') != 'verified' or self._restore_pending(data, aid)):
                continue
            path = (vault / association['path']).resolve()
            if path.is_relative_to(vault) and path.is_file():
                links.setdefault(aid, {})['obsidian'] = 'obsidian://open?' + urlencode({'path': str(path)})
        return links

    def _status(self, data, arxiv_id, aid):
        operations = [copy.deepcopy(op) for op in data['operations'].values()
                      if op['request']['paper']['arxiv_id'] == aid
                      and op['request']['profile_id'] == (self.config.profile_id or 'default')]
        operations.extend(self._legacy_receipts(aid))
        for op in operations:
            for step in op['targets'].values():
                if step.get('status') == 'running' and str(self.path) + op['operation_id'] not in _ACTIVE_OPERATIONS:
                    step.update(status='interrupted', error='上次收录已中断，可重试')
        operations.sort(key=lambda op: op.get('updated_at', op['created_at']), reverse=True)
        links = {}
        identity = data.get('zotero_libraries', {}).get(self.config.zotero.base_url)
        for association in data['associations'].values():
            if (association.get('target') == 'zotero' and association.get('arxiv_id') == aid
                    and association.get('base_url') == self.config.zotero.base_url
                    and association.get('library_id') == identity and association.get('status') == 'verified'):
                links['zotero'] = 'zotero://select/library/items/' + association['item_key']
        association = data['associations'].get(self._obsidian_key({'arxiv_id': aid}))
        if (not self._restore_pending(data, aid)
                and (not association or association.get('status') in {'verified', 'unlinked'})):
            with ObsidianExporter(self.config, self.project_root, clients=self.clients) as exporter:
                observed = exporter.inspect_paper_note(aid, (association or {}).get('path', ''))
            association = {**(association or {}), **observed}
        if association and association.get('path') and association.get('status') == 'verified':
            vault = Path(self.config.obsidian.vault_path).resolve()
            path = (vault / association['path']).resolve()
            if path.is_relative_to(vault) and path.is_file():
                links['obsidian'] = 'obsidian://open?' + urlencode({'path': str(path)})
        associations = {'obsidian': association or {'status': 'unlinked'}}
        for entry in data['associations'].values():
            if entry.get('target') == 'zotero' and entry.get('arxiv_id') == aid and entry.get('library_id') == identity:
                associations['zotero'] = entry
        if self._restore_pending(data, aid):
            links = {}
        expected = requested_version(arxiv_id)
        for entry in associations.values():
            entry['update_needed'] = bool(expected and (entry.get('path') or entry.get('item_key'))
                                          and entry.get('version') != expected)
        for op in operations:
            note = op['targets'].get('obsidian')
            if note and note.get('status') == 'succeeded':
                note['update_needed'] = bool(association and (
                    association.get('version') != op['request']['paper'].get('version')
                    or association.get('material_hashes') != op['request']['material_hashes']))
        return {'operations': operations, 'links': links, 'associations': associations}
