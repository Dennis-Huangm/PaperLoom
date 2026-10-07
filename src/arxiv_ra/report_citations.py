"""Optional source links. A location is not a verdict on a report's claims."""
from __future__ import annotations

import re
import unicodedata

from markdown_it import MarkdownIt

from .source_spans import source_spans, exact_span, normalize_grouped_source_ids

TOKEN = re.compile(r"\[\[证据:(.*?)\]\]|\[\[证据ID:((?:(?!\[\[)[^\n])*?)\]\]", re.S)


def unavailable_citations():
    return {'status': 'unavailable', 'citations': [], 'validated_citations': 0,
            'cited_pages': [], 'scope': 'source_location_only'}


def strip_tokens(report: str) -> str:
    return TOKEN.sub('', normalize_grouped_source_ids(report))


def _strip_model_pages(report: str) -> str:
    """Remove unregistered source-link pages without editing report content."""
    lines = report.splitlines(keepends=True)
    offsets = [0]
    for line in lines:
        offsets.append(offsets[-1] + len(line))
    protected = [(offsets[t.map[0]], offsets[t.map[1]])
                 for t in MarkdownIt('commonmark').parse(report)
                 if t.type in {'fence', 'code_block'} and t.map]
    protected.extend((m.start(), m.end()) for m in re.finditer(
        r'(?<!`)(`+)(?!`)[\s\S]*?(?<!`)\1(?!`)', report))
    links = re.compile(
        r'''(?P<prefix>\]\(\s*<?|<a\b[^>\n]*?\bhref\s*=\s*["'])(?P<url>paper\.pdf|https?://(?:www\.)?arxiv\.org/pdf/[^\s<>"'()#]+)#page\s*=\s*\d+''',
        re.I)
    def clean(match):
        if any(start <= match.start() < end for start, end in protected):
            return match[0]
        return match['prefix'] + match['url']
    return links.sub(clean, report)


def attach_citations(report: str, parsed, *, pdf_available: bool, full_report: bool):
    """Never withhold prose or change values because a citation is unavailable."""
    try:
        return _attach(report, parsed, pdf_available=pdf_available, full_report=full_report)
    except Exception:
        # All work here is optional, local and read-only. Cancellation derives
        # from BaseException and must still propagate; publication I/O is outside.
        return strip_tokens(report), unavailable_citations()


def _attach(report, parsed, *, pdf_available, full_report):
    normalize = lambda text: re.sub(r'\s+', '', unicodedata.normalize('NFKC', text))
    report = normalize_grouped_source_ids(report)
    # Model-written page fragments have no registered location. Keep the PDF
    # link itself; only the binder below supplies page-specific references.
    report = _strip_model_pages(report)
    pages = parsed.page_texts if parsed and pdf_available and full_report else []
    normalized = [normalize(page) for page in pages]
    spans = source_spans(parsed) if pages else {}
    citations, by_location, cache = [], {}, {}
    rejected = 0
    headings = list(re.finditer(r'(?m)^##\s+([^\n]+)', report))

    def replace(match):
        nonlocal rejected
        try:
            if match[2] is not None:
                key = match[2].strip()
                if key not in cache:
                    cache[key] = (spans.get(key) or exact_span(key, parsed)) if pages else None
                source = cache[key]
            else:
                quote = match[1]
                needle = normalize(quote)
                if needle not in cache:
                    matches = [i + 1 for i, page in enumerate(normalized) if needle and needle in page]
                    cache[needle] = ({'page': matches[0], 'quote': quote}
                                     if 20 <= len(needle) <= 600 and len(matches) == 1 else None)
                source = cache[needle]
            if source is None:
                rejected += 1
                return ''
            quote = ' '.join(source['quote'].split())
            key = (source['page'], quote)
            citation = by_location.get(key)
            if citation is None:
                citation = {'id': len(citations) + 1, 'page': source['page'], 'quote': quote, 'sections': []}
                citation.update({k: source[k] for k in ('source_id', 'start', 'end', 'text_sha256') if k in source})
                by_location[key] = citation
                citations.append(citation)
            section = next((h[1].strip() for h in reversed(headings) if h.start() < match.start()), '')
            if section and section not in citation['sections']:
                citation['sections'].append(section)
            return f'[{citation["id"]}](paper.pdf#page={citation["page"]})'
        except Exception:
            rejected += 1
            return ''

    output = TOKEN.sub(replace, report)
    return output, {'status': 'located' if citations else 'unavailable',
                    'scope': 'source_location_only', 'citations': citations,
                    'validated_citations': len(citations), 'rejected_citations': rejected,
                    'source_spans': {'available': len(spans), 'cited': sum('source_id' in c for c in citations)},
                    'parsed_pages': len(pages), 'total_pages': parsed.total_pages if parsed else None,
                    'cited_pages': sorted({c['page'] for c in citations})}
