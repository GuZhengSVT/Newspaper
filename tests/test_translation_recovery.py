import copy
import json
from datetime import datetime
from unittest.mock import Mock, patch

from fixtures import NewspaperCase
from newspaper.pipeline import generate
from newspaper.storage import Store
from newspaper.translation import (legacy_sentence_units, paragraph_pairs, sentence_units,
                                   resume_translations, translate, validate_bilingual, validate_dates)

class TranslationRecoveryTests(NewspaperCase):
    def article(self, text):
        article = self.doc['sections'][0]['articles'][0]
        article['facts'][0]['text'] = text
        return article

    def test_sentence_boundaries_do_not_split_punctuation_inside_work_titles(self):
        first = '《BanG Dream! It’s MyGO!!!!!》与《BanG Dream! Ave Mujica》的新作确定播出。'
        second = '电影《BanG Dream! Ave Mujica prima aurora》仍在上映。'
        article = self.article(first + second)
        self.assertEqual(sentence_units(article), [first, second])
        article['facts'][0]['text'] = '作品《外传「Why?!」》已公布。数值为3.14，NASA缩写不变！下一句。'
        self.assertEqual(sentence_units(article), ['作品《外传「Why?!」》已公布。', '数值为3.14，NASA缩写不变！', '下一句。'])

    def test_existing_archive_pairs_remain_readable_without_changing_chinese(self):
        text = '《BanG Dream! It’s MyGO!!!!!》宣布续作。'
        article = self.article(text)
        article['bilingual'] = {'title_en': 'Sequel announced', 'sentences': [
            {'zh': zh, 'en': 'Stored English segment.'} for zh in legacy_sentence_units(article)]}
        self.assertGreater(len(article['bilingual']['sentences']), len(sentence_units(article)))
        validate_bilingual(article, required=True)
        self.assertEqual(paragraph_pairs(article, [text])[0]['zh'], text)
        article['bilingual']['sentences'][0]['zh'] += '篡改'
        with self.assertRaises(ValueError):validate_bilingual(article)

    def test_faithful_dates_use_one_translation_without_a_model_reviewer(self):
        self.article('测试期为太平洋时间11月12日18时至18日17时59分。')
        draft = {'title_en': 'Test dates', 'sentences': [
            {'id': 0, 'en': 'The test runs from November 12 at 18:00 to November 18 at 17:59 Pacific Time.'}]}
        client = Mock();client.request.return_value = draft
        diagnostics = []
        self.assertEqual(translate(self.doc, client, diagnostics=diagnostics), 1)
        self.assertEqual([c.args[0] for c in client.request.call_args_list], ['translate'])
        self.assertEqual(self.doc['sections'][0]['articles'][0]['bilingual']['sentences'][0]['en'], draft['sentences'][0]['en'])
        self.assertEqual(diagnostics, [])

    def test_calendar_date_changes_fail_locally_without_another_model_call(self):
        text = '测试期为太平洋时间11月12日18时至18日17时59分。'
        self.article(text)
        wrong = {'title_en': 'Test dates', 'sentences': [
            {'id': 0, 'en': 'The test runs from November 12 at 18:00 to November 19 at 17:59 Pacific Time.'}]}
        client = Mock();client.request.return_value = wrong
        diagnostics = []
        with self.assertRaises(ValueError):translate(self.doc, client, diagnostics=diagnostics)
        client.request.assert_called_once()
        self.assertIn('11-19', diagnostics[0]['reason'])
        for english in ['November 12 to November 18', '12 Nov to 18 Nov', '2026-11-12 to 2026-11-18']:
            validate_dates(text, english)
        for english in ['November 12 to November 19', '12 Nov to 19 Nov', '2026-11-12 to 2026-11-19']:
            with self.assertRaises(ValueError):validate_dates(text, english)

    def test_invalid_model_json_or_truncation_does_not_generate_a_second_translation(self):
        import httpx
        import os
        from newspaper.model import Model
        request = httpx.Request('POST', 'https://example.test/chat/completions')
        for finish, content in [('stop', '{"sentences":['), ('length', '{"sentences":[]')]:
            with self.subTest(finish=finish), \
                 patch.dict(os.environ, {'NEWSPAPER_API_KEY': 'synthetic-test-secret'}), \
                 patch('newspaper.model.httpx.Client') as client:
                post = client.return_value.__enter__.return_value.post
                post.return_value = httpx.Response(200, request=request, json={
                    'choices': [{'finish_reason': finish, 'message': {'content': content}}]})
                with self.assertRaises(ValueError):Model(self.settings).request('translate', {})
                post.assert_called_once()

    def test_failed_article_resumes_from_checkpoint_after_restart_without_research(self):
        self.settings.values['translation'] = {'enabled': True}
        second = copy.deepcopy(self.doc['sections'][0]['articles'][0]);second['event_key'] = 'fixture|second|event'
        self.doc['sections'][0]['articles'].append(second)
        first_draft = {'title_en': 'First article', 'sentences': [{'id': 0, 'en': 'First approved translation.'}]}
        now = datetime.fromisoformat(self.day + 'T12:05:00+08:00')
        with patch('newspaper.pipeline.utcnow', return_value=now), \
             patch('newspaper.pipeline.gather', return_value={'sources': [self.source]}), \
             patch('newspaper.pipeline.Model.request', side_effect=[self.doc, {'ok': True}, first_draft, {}]):
            with self.assertRaises(ValueError):generate(self.settings, self.store, notify=False)
        checkpoint = self.settings.data / 'editions' / self.day / 'translation-checkpoint.json'
        saved = json.loads(checkpoint.read_text())['report']['sections'][0]['articles']
        self.assertIn('bilingual', saved[0]);self.assertNotIn('bilingual', saved[1])
        self.assertFalse(self.store.published(self.day))
        self.store.close();self.store = Store(self.settings)
        second_draft = {'title_en': 'Second article', 'sentences': [{'id': 0, 'en': 'Second approved translation.'}]}
        with patch('newspaper.pipeline.utcnow', return_value=now), patch('newspaper.pipeline.gather') as research, \
             patch('newspaper.pipeline.Model.request', side_effect=[second_draft]) as model:
            self.assertEqual(generate(self.settings, self.store, notify=False)['state'], 'published')
        research.assert_not_called()
        self.assertEqual([c.args[0] for c in model.call_args_list], ['translate'])
        published = self.store.document(self.day)['sections'][0]['articles']
        self.assertEqual(published[0]['bilingual']['title_en'], 'First article')
        self.assertEqual(published[1]['bilingual']['title_en'], 'Second article')
        self.assertFalse(checkpoint.exists())
        metrics = json.loads(self.store.db.execute('select metrics from runs order by id desc limit 1').fetchone()[0])
        self.assertTrue(metrics['resumed_translation'])

    def test_chinese_first_preserves_partial_translation_across_failed_rebuild(self):
        self.settings.values['translation'] = {'enabled': True, 'publish': 'chinese_first'}
        second = copy.deepcopy(self.doc['sections'][0]['articles'][0])
        second.update(event_key='fixture|second|event', title='另一则事件')
        second['facts'][0]['text'] = '另一项离线测试。'
        self.doc['sections'][0]['articles'].append(second)
        now = datetime.fromisoformat(self.day + 'T12:05:00+08:00')
        with patch('newspaper.pipeline.utcnow', return_value=now), \
             patch('newspaper.pipeline.gather', return_value={'sources': [self.source]}), \
             patch('newspaper.pipeline.Model.request', side_effect=[self.doc, {'ok': True}]) as model:
            self.assertEqual(generate(self.settings, self.store, notify=False)['state'], 'published')
        self.assertEqual([call.args[0] for call in model.call_args_list], ['edit', 'review'])
        self.store.db.execute("INSERT INTO deliveries(day,channel,state) VALUES(?,'email','sent')", (self.day,))
        self.store.db.commit()
        draft = {'title_en': 'Second article', 'sentences': [{'id': 0, 'en': 'Another offline fixture.'}]}
        with patch('newspaper.model.Model.request', side_effect=[{}, draft]), \
             patch('newspaper.publish.build', side_effect=OSError('build interrupted')):
            with self.assertRaises(OSError):
                resume_translations(self.settings, self.store)
        saved = self.store.document(self.day)['sections'][0]['articles']
        self.assertNotIn('bilingual', saved[0])
        self.assertEqual(saved[1]['bilingual']['title_en'], 'Second article')
        with patch('newspaper.model.Model.request') as model:
            result = resume_translations(self.settings, self.store)
        self.assertTrue(result['rebuilt'])
        self.assertEqual(result['translated_articles'], 0)
        self.assertEqual(result['deferred'], 1)
        model.assert_not_called()
        self.assertEqual(self.store.db.execute('SELECT state FROM deliveries').fetchone()[0], 'sent')

    def test_model_tokens_are_shared_by_manager_worker_and_translation(self):
        import httpx
        from newspaper.model import Model, ModelBudgetReached, model_usage

        self.settings.values['model']['max_tokens_per_edition'] = 10
        request = httpx.Request('POST', 'https://example.test/v1/chat/completions')
        response = httpx.Response(200, request=request, json={
            'usage': {'total_tokens': 11},
            'choices': [{'finish_reason': 'stop', 'message': {'content': '{}'}}]})
        with patch.dict('os.environ', {'NEWSPAPER_API_KEY': 'fixture'}), \
             patch('newspaper.model.httpx.Client') as factory:
            post = factory.return_value.__enter__.return_value.post
            post.return_value = response
            Model(self.settings, role='manager', store=self.store, edition_key=self.day).request('coordinate', {})
            for stage in ('edit', 'translate'):
                with self.assertRaises(ModelBudgetReached):
                    Model(self.settings, store=self.store, edition_key=self.day).request(stage, {})
            post.assert_called_once()
        self.assertEqual(model_usage(self.settings, self.store), [
            {'day': self.day, 'total_tokens': 11, 'unreported_calls': 0, 'limit': 10, 'blocked': True}])

    def test_provider_cooldown_deferrals_do_not_count_as_failed_probes(self):
        import httpx
        from newspaper.alerts import incidents
        from newspaper.model import Model, ModelDeferred, model_usage

        request = httpx.Request('POST', 'https://example.test/v1/chat/completions')
        unavailable = httpx.Response(503, request=request, headers={'Retry-After': '120'})
        with patch.dict('os.environ', {'NEWSPAPER_API_KEY': 'fixture'}), \
             patch('newspaper.model.httpx.Client') as factory, patch('newspaper.model.time.sleep') as sleep:
            post = factory.return_value.__enter__.return_value.post
            post.return_value = unavailable
            for pending in range(3):
                with self.assertRaises(httpx.HTTPStatusError):
                    Model(self.settings, store=self.store, edition_key=self.day).request('plan', {})
                with self.assertRaises(ModelDeferred):
                    Model(self.settings, store=self.store, edition_key=self.day).request('plan', {})
                self.assertEqual(post.call_count, pending + 1)
                self.store.db.execute('UPDATE model_backoff SET retry_at=0')
                self.store.db.commit()
            sleep.assert_not_called()
        self.assertEqual(model_usage(self.settings, self.store)[0]['unreported_calls'], 3)
        [(incident, body)] = incidents(self.store, 0)
        self.assertTrue(incident.startswith('model:'))
        self.assertIn('HTTP 503', body)
