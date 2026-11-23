import json
from unittest.mock import patch

import httpx

from fixtures import NewspaperCase
from newspaper.batches import prepare_waiting
from newspaper.publication import delivery_ready, sync_pages
from newspaper.publish import build
from newspaper.retention import clean
from newspaper.storage import atomic_json


class PublicationTests(NewspaperCase):
    def test_recovered_batch_excludes_issues_published_with_no_notify(self):
        for slug in ('daily', 'weekly'):
            self.save(self.issue(slug))
        expected = [self.day + '/' + slug for slug in ('daily', 'weekly')]
        self.store.db.execute('UPDATE editions SET notify_suppressed=1 WHERE day=?', (expected[0],))
        self.store.db.execute('''INSERT INTO notification_batches
            (id,task_id,period,window_start,window_end,expected,channels,state,expires_at)
            VALUES('batch','group','daily',?,?,?,'["email"]','waiting',?)''',
            (self.window['window_start'], self.window['window_end'], json.dumps(expected), 9999999999))
        prepare_waiting(self.settings, self.store)
        batch = self.store.db.execute("SELECT * FROM notification_batches WHERE id='batch'").fetchone()
        self.assertEqual(json.loads(batch['expected']), expected[1:])
        self.assertEqual(batch['state'], 'ready')

    def test_idle_cleanup_leaves_release_and_database_in_place(self):
        self.save()
        release = build(self.settings, self.store)
        statements = []
        self.store.db.set_trace_callback(statements.append)
        with patch('newspaper.retention.build') as render:
            result = clean(self.settings, self.store)
        render.assert_not_called()
        self.assertNotIn('VACUUM', [s.strip().upper() for s in statements])
        self.assertEqual(result['release'], release['release'])
        self.assertFalse(result['changed'])

    def test_identical_build_preserves_release_and_explicit_build_repairs_damage(self):
        self.save()
        first = build(self.settings, self.store)
        second = build(self.settings, self.store)
        self.assertFalse(second['changed'])
        self.assertEqual(first['release'], second['release'])
        (self.settings.web / 'current/index.html').write_text('damaged')
        repaired = build(self.settings, self.store)
        self.assertTrue(repaired['changed'])
        self.assertEqual(first['version'], repaired['version'])

    def test_public_version_and_edition_payload_gate_remote_delivery(self):
        self.save()
        self.settings.values['publication'] = {'mode': 'pages', 'repository': 'example/newspaper'}
        version = build(self.settings, self.store)['version']
        self.assertFalse(delivery_ready(self.settings, self.store, [self.day]))
        with patch('newspaper.publication.httpx.Client') as factory, \
             patch.dict('os.environ', NEWSPAPER_PAGES_TOKEN='synthetic'):
            client = factory.return_value.__enter__.return_value
            client.get.return_value = httpx.Response(404)
            self.assertEqual(sync_pages(self.settings)['state'], 'awaiting_publication')
            sync_pages(self.settings)
            client.post.assert_called_once()
            self.assertFalse(delivery_ready(self.settings, self.store, [self.day]))
            request = client.post.call_args.kwargs['json']['inputs']
            client.get.return_value = httpx.Response(200, json=request)
            self.assertEqual(sync_pages(self.settings)['state'], 'live')
            self.assertTrue(delivery_ready(self.settings, self.store, [self.day]))
            factory.reset_mock()
            sync_pages(self.settings)
            factory.assert_not_called()
        report = self.settings.data / 'editions' / self.day / 'report.json'
        doc = json.loads(report.read_text())
        doc['title'] = 'Corrected title'
        atomic_json(report, doc)
        self.assertFalse(delivery_ready(self.settings, self.store, [self.day]))

    def test_reverting_a_release_waits_for_its_own_publication(self):
        self.save()
        self.settings.values['publication'] = {'mode': 'pages', 'repository': 'example/newspaper'}
        original = build(self.settings, self.store)['version']
        with patch('newspaper.publication.httpx.Client') as factory, \
             patch.dict('os.environ', NEWSPAPER_PAGES_TOKEN='synthetic'):
            client = factory.return_value.__enter__.return_value
            sync_pages(self.settings)
            first = client.post.call_args.kwargs['json']['inputs']
            client.get.return_value = httpx.Response(200, json=first)
            self.assertEqual(sync_pages(self.settings)['state'], 'live')
            path = self.settings.data / 'editions' / self.day / 'report.json'
            atomic_json(path, {**self.doc, 'title': 'Revision B'})
            build(self.settings, self.store)
            sync_pages(self.settings)
            atomic_json(path, self.doc)
            self.assertEqual(build(self.settings, self.store)['version'], original)
            self.assertEqual(sync_pages(self.settings)['state'], 'awaiting_publication')
            last = client.post.call_args.kwargs['json']['inputs']
            self.assertNotEqual(first['request_id'], last['request_id'])
            self.assertEqual(sync_pages(self.settings)['state'], 'awaiting_publication')
            client.get.return_value = httpx.Response(200, json=last)
            self.assertEqual(sync_pages(self.settings)['state'], 'live')
