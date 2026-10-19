"""Bounded source contexts and claim-level evidence bundles; no model calls."""
from __future__ import annotations

import re


def context(text: str, terms: list[str], limit: int = 6500) -> str:
    if len(text) <= limit:
        return text
    ranges = [(0, min(2200, limit))]
    folded = text.casefold()
    for term in terms:
        if len(term) < 3:
            continue
        for match in list(re.finditer(re.escape(term.casefold()), folded))[:3]:
            ranges.append((max(0, match.start() - 450), min(len(text), match.end() + 650)))
    merged = []
    for start, end in sorted(ranges):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
        else:
            merged.append((start, end))
    return "\n[…]\n".join(text[a:b] for a, b in merged)[:limit]


def selected_sources(sources: list[dict], candidates: list[dict]) -> list[dict]:
    terms = {}
    for candidate in candidates:
        for sid in candidate['source_ids']:
            terms.setdefault(sid, []).extend(candidate.get('entities', []) + [candidate.get('focus', '')])
    return [{**s, 'content': context(s['content'], terms[s['id']])} for s in sources if s['id'] in terms]


def used_evidence(doc: dict, sources: list[dict]) -> list[dict]:
    """Only verbatim cited snippets and short adjacent contexts survive the run."""
    by_id = {s['id']: s for s in sources}
    quotes = {}
    for section in doc['sections']:
        for article in section['articles']:
            for fact in article['facts']:
                for evidence in fact['evidence']:
                    quotes.setdefault(evidence['source_id'], set()).add(evidence['quote'])
    result = []
    for sid, items in quotes.items():
        source = by_id[sid]
        snippets = []
        for quote in sorted(items):
            # Validation normalizes whitespace; use a whitespace-insensitive locator too.
            match = re.search(r'\s+'.join(re.escape(p) for p in quote.split()), source['content'], re.I)
            if match:
                snippets.append(source['content'][max(0, match.start()-250):match.end()+250])
        result.append({k: v for k, v in source.items() if k != 'content'} |
                      {'content': '\n[…]\n'.join(dict.fromkeys(snippets)), 'quotes': sorted(items)})
    return result


def validate_selection(selection: dict, settings, sources: list[dict], limit: int) -> list[dict]:
    candidates = selection.get('candidates')
    if not isinstance(candidates, list) or len(candidates) > limit:
        raise ValueError('Invalid candidate budget')
    ids = {s['id'] for s in sources}
    sections = {s['id'] for s in settings.sections}
    for item in candidates:
        if (item.get('section_id') not in sections or item.get('importance') not in {'重大','重要'}
            or not isinstance(item.get('event_key'), str) or not item['event_key']
            or not isinstance(item.get('entities'), list) or not item['entities']
            or any(not isinstance(e,str) or not e or len(e)>120 for e in item['entities'])
            or not isinstance(item.get('source_ids'),list) or not item['source_ids']
            or not set(item['source_ids']) <= ids):
            raise ValueError('Invalid selected event')
    return candidates
