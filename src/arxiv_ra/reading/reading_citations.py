"""Resolve reading citations against registered evidence, before Markdown.

Evidence identity is authoritative. Model punctuation is only presentation:
an exact registered ID in prose or an ID-only inline code span is a citation.
Code examples and URLs remain literal. No page or excerpt is inferred from
an unregistered ID.
"""
import re


SOURCE_MARKER = re.compile(r'\[\[来源:([^\]\r\n]*)\]\]')
SOURCE_GROUP = re.compile(r'\[(\[来源:[^\[\]\r\n]+\](?:\s*[,，、]\s*\[(?:来源:)?[^\[\]\r\n]+\])+)\]')
SINGLE_SOURCE_MARKER = re.compile(r'(?<!\[)\[来源:([^\[\]\r\n]+)\](?!\])')
BARE_SOURCE_IDS = r'`?[a-f0-9]{16}`?(?:\s*[,，、]\s*`?[a-f0-9]{16}`?)*'
SOURCE_TOKEN = re.compile(
    r'(?P<marker>\[\[来源:[^\]\r\n]*\]\])'
    r'|(?P<literal>(?P<ticks>`+)[\s\S]*?(?P=ticks)(?!`)|~{3,}[\s\S]*?~{3,}'
    r'|!?\[[^\]\r\n]*\]\([^\r\n]*?\)|https?://[^\s<>]+)'
    rf'|\\\(\s*(?:\(\s*)?{BARE_SOURCE_IDS}\s*(?:\)\s*)?\\\)'
    rf'|\$\s*(?:\(\s*)?{BARE_SOURCE_IDS}\s*(?:\)\s*)?\$'
    rf'|\(\s*{BARE_SOURCE_IDS}\s*\)|（\s*{BARE_SOURCE_IDS}\s*）'
    r'|(?P<id>(?<![a-zA-Z0-9_-])[a-f0-9]{16}(?![a-zA-Z0-9_-]))')


def _pending_tail(text, known_ids, streaming):
    """Keep an unfinished protocol token out of the visible streamed answer."""
    replacement = '' if streaming else '（引用未完成）'
    for opening in ('[[', '['):
        start = text.rfind(opening)
        if start < 0:
            continue
        tail = text[start:]
        prefix = opening + '来源:'
        if (prefix.startswith(tail) and len(tail) > len(opening)) or (
                tail.startswith(prefix) and (']' * len(opening)) not in tail[len(prefix):]):
            return text[:start] + replacement
    # Providers can split bare IDs at any character boundary too. Wait for
    # enough characters to identify a registered ID prefix unambiguously.
    match = re.search(r'`?\b([a-f0-9]{6,15})$', text)
    if match and any(cid.startswith(match[1]) for cid in known_ids):
        return text[:match.start()] + replacement
    return text


def normalize_source_markers(text, known_ids=(), *, streaming=False):
    """Canonicalize evidence references; retain unknown hashes as ordinary text."""
    known_ids = set(known_ids)
    text = _pending_tail(text, known_ids, streaming)

    def expand_group(match):
        return re.sub(r'\[(?:来源:)?([^\[\]\r\n]+)\]',
                      lambda item: '[[来源:' + item[1].strip() + ']]', match[1])

    text = SOURCE_GROUP.sub(expand_group, text)
    text = SINGLE_SOURCE_MARKER.sub(lambda match: '[[来源:' + match[1] + ']]', text)

    def resolve(match):
        if match['marker'] is not None:
            return match[0]
        if match['literal'] is not None:
            ticks = match['ticks']
            # A lone citation wrapped in backticks is common provider output.
            # Longer code expressions and fenced code blocks remain literal.
            if not ticks or len(ticks) >= 3:
                return match[0]
            inner = match[0][len(ticks):-len(ticks)].strip()
            if not re.fullmatch(BARE_SOURCE_IDS, inner):
                return match[0]
        ids = re.findall(r'[a-f0-9]{16}', match[0])
        if not known_ids.intersection(ids):
            return match[0]
        return '、'.join('[[来源:' + cid + ']]' for cid in ids)

    return SOURCE_TOKEN.sub(resolve, text) if known_ids else text


def registered_evidence(message):
    """Read the durable registry, with compatibility for pre-registry messages."""
    return {c['id']: c for c in [*message.get('evidence', []), *message.get('citations', [])]}


def prepare_source_text(text, known_ids=(), *, streaming=False):
    # Provider URLs are not evidence. Resolve the remaining label against our
    # registry so a model-written [ID](URL) cannot bypass citation resolution.
    text = re.sub(r'!?\[([^\]]*)\]\([^)]*\)', r'\1', text)
    return normalize_source_markers(text, known_ids, streaming=streaming)


def recover_legacy_evidence(root, user, message):
    """Reproduce lost legacy records from completed tools and cached PDFs only.

    Older finalization discarded records when citation parsing failed. Replay
    only recorded read/search operations; a record is recovered only when its
    recomputed content hash equals an ID actually present in the old answer.
    No downloads, model calls, or page guesses are permitted in this migration.
    """
    if 'evidence' in message or message.get('role') != 'assistant':
        return []
    targets = set(re.findall(r'\b[a-f0-9]{16}\b', message.get('text', '')))
    targets.difference_update(registered_evidence(message))
    if not targets:
        return []
    import hashlib
    from .reading_sources import ReadingSources

    references = user.get('references') or []
    recovered = {}
    for reference in references:
        paper, report = reference.get('paper') or {}, reference.get('report') or {}
        if not paper.get('arxiv_id') or type(paper.get('version')) is not int:
            continue
        pid = f"{paper['arxiv_id']}v{paper['version']}"
        source_id = report.get('source_id') or hashlib.sha256(pid.encode()).hexdigest()
        if not isinstance(source_id, str) or not re.fullmatch(r'[a-f0-9]{64}', source_id):
            continue
        pdf = root / '.reading' / 'sources' / f'{source_id}.pdf'
        if not pdf.is_file():
            continue
        source = ReadingSources(root, {'paper': paper}, {'report': report}, None, lambda **fields: None)
        source.source_id, source.pdf = source_id, pdf
        source.source_note = message.get('material', {}).get('source_note', '')
        for step in message.get('steps', []):
            if step.get('status') != 'completed' or step.get('paper_id') != pid:
                continue
            label = step.get('label', '')
            try:
                if step['name'] == 'search' and '检索原文：' in label:
                    source.execute('search', {'query': label.split('检索原文：', 1)[1]})
                elif step['name'] == 'read_pages':
                    pages = re.search(r'读取原文第 (\d+)–(\d+) 页', label)
                    if pages:
                        source.execute('read_pages', {'start': int(pages[1]), 'end': int(pages[2])})
            except (OSError, RuntimeError, ValueError):
                # Missing/corrupted material or a truncated old tool description
                # cannot establish evidence. Preserve the original answer.
                continue
        for item in source.citations:
            if item['id'] in targets:
                recovered[item['id']] = {**item, 'paper_id': pid, 'paper_title': paper['title']}
    return list(recovered.values())
