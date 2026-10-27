"""One compact Max coordinator per board; isolated low-effort task workers."""
from __future__ import annotations

import json

from .model import Model


class Coordinator:
    def __init__(self, settings, edition, window, worker):
        self.model = Model(settings, role='manager', group_id=edition.get('group_id', edition['id']))
        self.bind(settings, edition, window, worker)

    def bind(self, settings, edition, window, worker):
        """Reuse this board's coordinator for its next serial publication window."""
        if self.model.group_id != edition.get('group_id', edition['id']):
            raise ValueError('A coordinator cannot manage another board')
        self.settings = settings
        self.edition = edition
        self.window = window
        self.worker = worker
        self.model.usage = worker.usage
        self.state = {}

    def start(self):
        scope_limit = max(80, 4000 // max(1, len(self.settings.sections)))
        plan = self.model.request('coordinate', {
            'phase': 'delegate', 'board': self.edition.get('group_id', self.edition['id']),
            'window': self.window,
            'sections': [{'id': s['id'], 'name': s['name'], 'scope': s.get('scope', '')[:scope_limit],
                          'filter': s.get('filter', {})} for s in self.settings.sections],
            'worker_tasks': ['search', 'organize', 'judge', 'translate'],
            'max_parallel_workers': 1})
        briefs = plan.get('section_briefs')
        expected = {s['id'] for s in self.settings.sections}
        if (not isinstance(briefs, list) or len(briefs) != len(expected)
            or any(not isinstance(b, dict) or set(b) != {'section_id', 'instruction'}
                   or b['section_id'] not in expected or not isinstance(b['instruction'], str)
                   or not 1 <= len(b['instruction']) <= 500 for b in briefs)
            or {b['section_id'] for b in briefs} != expected):
            raise ValueError('Coordinator must delegate every configured section exactly once')
        self.state = {'section_briefs': briefs}
        self.worker.delegation = json.dumps(briefs, ensure_ascii=False)

    def approve(self, doc, metrics):
        if self.state.get('approved'):return None
        articles = [a for section in doc['sections'] for a in section['articles']]
        decision = self.model.request('coordinate', {
            'phase': 'publish', 'board': self.edition.get('group_id', self.edition['id']),
            'window': self.window, 'delegation': self.state.get('section_briefs', []),
            'worker_results': [{'section_id': s['id'], 'articles': [
                {'event_key': a['event_key'], 'title': a['title'][:200], 'summary': a['summary'][:240],
                 'importance': a.get('importance'), 'reliability': a.get('reliability'),
                 'verification': a['verification']} for a in s['articles']],
                'empty_reason': s.get('empty_reason', '')[:200]} for s in doc['sections']],
            'fact_checks_passed': True, 'fetched_sources': metrics.get('fetched_sources', 0),
            'coverage_notes': doc.get('coverage_notes', [])[:5]})
        keys = decision.get('keep_event_keys')
        expected = {a['event_key'] for a in articles}
        if (decision.get('action') != 'publish' or not isinstance(keys, list)
            or any(not isinstance(k, str) for k in keys) or len(set(keys)) != len(keys)
            or not set(keys) <= expected):
            raise ValueError('Coordinator rejected publication or returned unknown articles')
        self.state['approved'] = True
        self.state['keep_event_keys'] = keys
        return set(keys)
