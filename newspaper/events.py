"""Link events across retained editions while keeping the evidence attached to each article."""
from __future__ import annotations

import hashlib
from datetime import datetime

from .research import source_url
from .storage import normalize_entity
from .validation import normalize


def event_evidence(article: dict, urls: dict) -> set[tuple[str, str]]:
    return {(source_url(urls[e['source_id']]),
             hashlib.sha256(normalize(e['quote']).encode()).hexdigest())
            for fact in article['facts'] if fact.get('kind', 'fact') == 'fact'
            for e in fact['evidence'] if e['source_id'] in urls}


def core_claims(article: dict) -> set[str]:
    return {hashlib.sha256(normalize(f['text']).encode()).hexdigest()
            for f in article['facts'] if f.get('kind', 'fact') == 'fact'}


def same_article_event(article: dict, previous: dict, source_urls: dict) -> bool:
    if normalize(article['event_key']) == normalize(previous['event_key']):
        return True
    entities = {normalize_entity(e) for e in article.get('entities', [])}
    old_entities = {normalize_entity(e) for e in previous.get('entities', [])}
    return bool(entities & old_entities and core_claims(article) == core_claims(previous)
                and event_evidence(article, source_urls) & event_evidence(previous, source_urls))


def link_report_events(doc: dict, history: list[dict], sources: list[dict],
                       *, task_id: str | None = None, candidates: list[dict] | None = None) -> None:
    urls = {s['id']: s['url'] for s in sources}
    fresh_urls = {s['id']: s['url'] for s in sources if not s.get('prior_edition')}
    selected = {c['event_key']: c for c in candidates or []}
    window_end = datetime.fromisoformat(doc['window_end'])
    edition = doc['date'] + ('/'+doc['edition'] if doc.get('edition') else '')
    for section in doc['sections']:
        for article in section['articles']:
            key = normalize(article['event_key'])
            evidence = event_evidence(article, urls)
            claims = core_claims(article)
            entities = {normalize_entity(e) for e in article.get('entities', [])}
            candidate = selected.get(article['event_key'], {})
            focus = normalize_entity(candidate.get('focus') or article['title'])
            matches = []
            for old in history:
                if old['day'] == edition or old['day'][:10] > doc['date']:
                    continue
                if old.get('window_end') and datetime.fromisoformat(old['window_end']) > window_end:
                    continue
                shared_evidence = evidence & {tuple(e) for e in old.get('evidence', [])}
                old_claims = set(old.get('claims', []))
                exact_key = key == normalize(old['id'])
                same_focus = focus and focus in {old.get('focus'), normalize_entity(old['description'])}
                same_entities = entities & {normalize_entity(e) for e in old.get('entities', [])}
                # A shared page or quotation may cover several unrelated events.
                matching_claims = claims == old_claims or (same_focus and bool(claims & old_claims))
                if exact_key or (same_entities and shared_evidence and matching_claims):
                    matches.append((old, old_claims))
            article.pop('event_links', None)
            if not matches:
                if article.get('follow_up') is True:
                    article['follow_up'] = False
                    article['new_facts'] = ''
                continue
            same_task = [(old, facts) for old, facts in matches if old.get('task_id') == task_id]
            baseline = same_task or matches
            all_old_claims = {claim for _, facts in baseline for claim in facts}
            cited = {item for old, _ in baseline for item in map(tuple, old.get('evidence', []))}
            has_new_claim = bool(claims - all_old_claims)
            has_new_evidence = bool(event_evidence(article, fresh_urls) - cited)
            progress = bool(article['follow_up'] and article['new_facts'].strip()
                            and has_new_claim and has_new_evidence)
            article['follow_up'] = progress
            if not progress:
                article['new_facts'] = ''
            prior = baseline[0][0]
            article['event_links'] = [{
                'edition': prior['day'], 'event_key': prior['id'],
                'kind': 'progress' if progress else 'related',
                'same_task': bool(same_task),
            }]


def editorial_history(history: list[dict]) -> list[dict]:
    fields = ('id', 'day', 'description', 'facts', 'entities', 'window_end', 'same_task')
    return [{key: row[key] for key in fields if key in row} for row in history]


def update_coverage(metrics: dict, doc: dict, stage: str) -> None:
    sections = metrics.setdefault('sections', {})
    for section in doc['sections']:
        sections.setdefault(section['id'], {})[stage] = len(section['articles'])
