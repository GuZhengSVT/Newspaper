from __future__ import annotations

import copy
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

from newspaper.config import Settings
from newspaper.pipeline import generate
from newspaper.publish import build, markdown
from newspaper.style import style_issues

from fixtures import NewspaperCase

class ReadingTests(NewspaperCase):

    def test_body_and_sources_are_rendered_once_without_editorial_boilerplate(self):
        doc = self.issue('culture')
        article = doc['sections'][0]['articles'][0]
        article['facts'].append(copy.deepcopy(article['facts'][0]))
        article['limitations'] = 'INTERNAL-LIMITATION'
        doc['coverage_notes'] = ['INTERNAL-COVERAGE']
        self.save(doc)
        build(self.settings, self.store)
        page = (self.settings.web/'current/daily'/self.day/'culture/index.html').read_text()
        body = page.split('<div class="news-body chinese-body">')[1].split('</div>')[0]
        self.assertNotIn('<a ', body)
        self.assertEqual(body.count('离线测试文本'), 1)
        self.assertEqual(page.count('href="https://example.test/source"'), 1)
        self.assertGreater(page.index('class="story-sources"'), page.index(body))
        self.assertNotIn('INTERNAL-LIMITATION', page)
        self.assertNotIn('INTERNAL-COVERAGE', page)
        md = markdown(doc)
        self.assertEqual(md.count('离线测试文本'), 1)
        self.assertEqual(md.count('https://example.test/source'), 1)
        self.assertTrue(md.rstrip().endswith('[example.test](<https://example.test/source>)'))

    def test_home_shows_todays_bodies_and_archives_keep_previous_issues(self):
        for slug, text in [('research', '今日研究正文'), ('culture', '今日Culture正文')]:
            doc = self.issue(slug)
            doc['sections'][0]['articles'][0]['facts'][0]['text'] = text
            self.save(doc)
        old = self.issue('culture')
        old['date'] = (datetime.fromisoformat(self.day) - timedelta(days=1)).date().isoformat()
        old['sections'][0]['articles'][0]['facts'][0]['text'] = 'YESTERDAY-BODY'
        self.save(old)
        build(self.settings, self.store)
        home = (self.settings.web/'current/index.html').read_text()
        for text in ['今日研究正文', '今日Culture正文', '/archive/culture/', '/archive/research/']:
            self.assertIn(text, home)
        self.assertNotIn('YESTERDAY-BODY', home)
        self.assertIn('id="contents-panel"', home)
        self.assertIn('今日Culture正文', (self.settings.web/'current/search.json').read_text())
        self.assertIn(f'/daily/{old["date"]}/culture/', (self.settings.web/'current/archive/culture/index.html').read_text())
        self.assertNotIn('/research/', (self.settings.web/'current/archive/culture/index.html').read_text().split('id="archive-list"')[1])
        build(self.settings, self.store, datetime.fromisoformat(self.day+'T12:00:00+08:00').timestamp()+86400)
        tomorrow = (self.settings.web/'current/index.html').read_text()
        self.assertEqual(tomorrow.count('今日尚未发布。'), 2)
        self.assertNotIn('今日Culture正文', tomorrow)

    def test_length_rules_leave_room_for_scientific_explanations(self):
        doc = self.issue('culture')
        article = doc['sections'][0]['articles'][0]
        article['facts'][0]['text'] = '字' * 421
        self.assertEqual(len(style_issues(doc)), 1)
        article['importance'] = '重大'
        self.assertEqual(style_issues(doc), [])
        article['facts'][0]['text'] = '字' * 651
        self.assertEqual(len(style_issues(doc)), 1)
        doc['sections'][0]['id'] = 'science'
        self.assertEqual(style_issues(doc), [])
        article['facts'][0]['text'] = '字' * 1101
        self.assertEqual(len(style_issues(doc)), 1)

    def test_pipeline_shortens_only_long_article_before_evidence_review(self):
        doc = self.issue('culture')
        long = doc['sections'][0]['articles'][0]
        short = {**copy.deepcopy(long), 'event_key': 'other-event', 'title': '保留新闻'}
        long['facts'][0]['text'] = '过长正文' * 110
        doc['sections'][0]['articles'].append(short)
        replacement = copy.deepcopy(long)
        replacement['facts'][0]['text'] = '压缩后的事实'
        repair = {'articles':[{'event_key':long['event_key'],'facts':[{'index':0,'text':'压缩后的事实'}]}]}
        candidates = [{'event_key': a['event_key'], 'entities': ['Fixture Product'], 'section_id': 'fixture',
                       'importance': '重要', 'source_ids': ['s1']} for a in [long, short]]
        settings = Settings({**self.settings.values, 'editions': [{'id': 'culture', 'name': 'Culture', 'sections': ['fixture']}],
                             'search': {'mode': 'bounded'}}, self.settings.data, self.settings.web, self.settings.base_url)
        review = {'ok': True, 'summary_ok': True, 'article_reviews': [
            {'event_key': a['event_key'], 'ok': True, 'problems': []} for a in [long, short]]}
        with patch('newspaper.pipeline.utcnow', return_value=datetime.fromisoformat(self.day+'T12:05:00+08:00')), \
             patch('newspaper.pipeline.bounded_research', return_value=([self.source], candidates, [], [], {})), \
             patch('newspaper.pipeline.Model.request', side_effect=[doc, repair, review]) as model:
            result = generate(settings, self.store, notify=False)
        self.assertEqual(result['state'], 'published')
        self.assertEqual([call.args[0] for call in model.call_args_list], ['edit', 'compress', 'review'])
        payload = model.call_args_list[1].args[1]
        self.assertEqual([a['event_key'] for a in payload['articles']], [long['event_key']])
        final = self.store.document(self.day+'/culture')['sections'][0]['articles']
        self.assertEqual(final[0]['facts'],replacement['facts'])
        self.assertEqual(final[1],short)


if __name__ == '__main__':
    unittest.main()
