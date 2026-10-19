from __future__ import annotations

import json
import unittest
from unittest.mock import Mock, patch

import test_newspaper as fixtures
from newspaper.config import Settings
from newspaper.bounded import research
from newspaper.discovery import discover, recent_links


class SourceFirstTests(unittest.TestCase):
    setUp=fixtures.NewspaperTests.setUp
    tearDown=fixtures.NewspaperTests.tearDown

    def index(self, sources=None, **kwargs):
        return {'section_id':'fixture','index_url':'https://example.test/feed','index_status':'links',
                'engines_ok':1,'sources':sources if sources is not None else [
                    {'url':self.source['url'],'title':'Synthetic release','snippet':'Evidence lead',
                     'discovery_index':'https://example.test/feed'}],**kwargs}

    def candidate(self):
        return {'event_key':'fixture|release|one','entities':['Fixture'],'section_id':'fixture',
                'importance':'重要','source_ids':['s1']}

    def test_reads_sources_before_planning_and_searches_only_a_named_gap(self):
        order=[]
        client=Mock()
        def request(stage,payload):
            order.append(stage)
            if stage=='plan':
                self.assertEqual(payload['strategy'],'source_first')
                self.assertEqual(payload['mandatory_section_ids'],[])
                self.assertIn('Synthetic release',payload['source_coverage'][0]['titles'])
                return {'queries':[{'section_id':'fixture','query':'independent announcement missing from feed'}]}
            return {'candidates':[self.candidate()]}
        client.request.side_effect=request
        def direct(*args):order.append('discover');return [self.index()],{}
        def search(*args):order.append('search');return []
        with patch('newspaper.bounded.discover',side_effect=direct),patch('newspaper.bounded.search',side_effect=search), \
             patch('newspaper.bounded.fetch',return_value=[self.source]):
            *_,metrics=research(self.settings,client,self.window,self.root,{})
        self.assertEqual(order,['discover','plan','search','select'])
        self.assertEqual(metrics['plugin_queries'],1)

    def test_source_only_mode_avoids_both_planner_and_search_calls(self):
        self.settings.values['discovery']={'max_gap_queries':0}
        client=Mock();client.request.return_value={'candidates':[self.candidate()]}
        with patch('newspaper.bounded.discover',return_value=([self.index()],{})), \
             patch('newspaper.bounded.search') as search,patch('newspaper.bounded.fetch',return_value=[self.source]):
            *_,metrics=research(self.settings,client,self.window,self.root,{})
        self.assertEqual([c.args[0] for c in client.request.call_args_list],['select'])
        search.assert_not_called();self.assertEqual(metrics['plugin_queries'],0)

    def test_a_read_source_without_recent_links_still_needs_a_search(self):
        client=Mock();client.request.side_effect=[{'queries':[{'section_id':'fixture','query':'recent release'}]},
                                                  {'candidates':[self.candidate()]}]
        empty=self.index([],index_status='no_recent_links')
        with patch('newspaper.bounded.discover',return_value=([empty],{})), \
             patch('newspaper.bounded.search',return_value=[self.index()]) as search, \
             patch('newspaper.bounded.fetch',return_value=[self.source]):
            *_,metrics=research(self.settings,client,self.window,self.root,{})
        self.assertEqual(client.request.call_args_list[0].args[1]['mandatory_section_ids'],['fixture'])
        search.assert_called_once()

    def test_failed_sources_cannot_be_counted_as_covered_to_skip_search(self):
        client=Mock();client.request.return_value={'queries':[]}
        bad=self.index([],engines_ok=0,index_status='CalledProcessError')
        with patch('newspaper.bounded.discover',return_value=([bad],{})),patch('newspaper.bounded.search') as search:
            with self.assertRaisesRegex(ValueError,'missing section'):research(self.settings,client,self.window,self.root,{})
        self.assertEqual(client.request.call_args.args[1]['mandatory_section_ids'],['fixture'])
        search.assert_not_called()

    def test_source_failure_uses_normal_search_fallback(self):
        client=Mock();client.request.side_effect=[{'queries':[{'section_id':'fixture','query':'release'}]},
                                                  {'candidates':[self.candidate()]}]
        bad=self.index([],engines_ok=0,index_status='CalledProcessError')
        with patch('newspaper.bounded.discover',return_value=([bad],{})), \
             patch('newspaper.bounded.search',return_value=[self.index()]) as search, \
             patch('newspaper.bounded.fetch',return_value=[self.source]):
            *_,metrics=research(self.settings,client,self.window,self.root,{})
        search.assert_called_once();self.assertEqual(metrics['source_covered_sections'],[])

    def test_bounded_previews_keep_publishers_fair_and_shorten_snippets(self):
        self.settings.values['discovery']={'max_previews_per_section':2,'max_gap_queries':0}
        first=[{'url':self.source['url'] if i==0 else f'https://example.test/a{i}','title':'First publisher','snippet':'x'*1000} for i in range(3)]
        second=[{'url':f'https://second.test/b{i}','title':'Second publisher','snippet':'y'*1000} for i in range(3)]
        client=Mock();client.request.return_value={'candidates':[self.candidate()]}
        with patch('newspaper.bounded.discover',return_value=([self.index(first),self.index(second)],{})), \
             patch('newspaper.bounded.fetch',return_value=[self.source]):
            *_,metrics=research(self.settings,client,self.window,self.root,{})
        previews=client.request.call_args.args[1]['sources']
        self.assertEqual([p['title'] for p in previews],['First publisher','Second publisher'])
        self.assertTrue(all(len(p['content'])<=350 for p in previews))
        self.assertEqual(metrics['preview_urls_omitted'],4)

    def test_one_url_shared_by_sections_is_read_once_and_disabled_sources_are_skipped(self):
        sections=[{'id':sid,'name':sid,'sources':[{'url':'https://example.test/feed','kind':'rss'}]} for sid in ['fixture','other']]
        sections[0]['sources'].append({'url':'https://disabled.test/feed','enabled':False})
        settings=Settings({**self.settings.values,'sections':sections},self.settings.data,self.settings.web,self.settings.base_url)
        feed=f'<rss><channel><item><title>Release</title><link>{self.source["url"]}</link><pubDate>{self.day}</pubDate></item></channel></rss>'
        with patch('newspaper.discovery.fetch_page',return_value={'kind':'html','content':feed}) as fetch:
            results,metrics=discover(settings,self.window,self.root)
        fetch.assert_called_once()
        self.assertEqual([r['section_id'] for r in results],['fixture','other'])
        self.assertEqual(metrics['discovery_requests'],1)
        self.assertEqual(metrics['discovery_indexes_ok'],1)

    def test_exceeding_source_budget_is_explicit_instead_of_skipping_later_sections(self):
        self.settings.values['discovery']={'max_indexes_per_edition':1}
        self.settings.values['sections'][0]['sources']=[{'url':f'https://example.test/{i}'} for i in range(2)]
        with patch('newspaper.discovery.fetch_page') as fetch:
            with self.assertRaisesRegex(ValueError,'silently skipped'):discover(self.settings,self.window,self.root)
        fetch.assert_not_called()

    def test_html_or_challenge_returned_for_rss_is_a_failure(self):
        self.settings.values['sections'][0]['sources']=[{'url':'https://example.test/feed','kind':'rss'}]
        with patch('newspaper.discovery.fetch_page',return_value={'kind':'html','content':'<html>Just a moment</html>'}):
            results,_=discover(self.settings,self.window,self.root)
        self.assertEqual(results[0]['engines_ok'],0)
        self.assertEqual(results[0]['index_status'],'ValueError')

    def test_embedded_json_and_custom_link_patterns_expose_static_company_announcements(self):
        data=json.dumps({'newsList':[{'title':'Synthetic device release','link':'/press/device-1','date':self.day}]})
        html='<div data-config="'+data.replace('"','&quot;')+'"></div>'
        links=recent_links(html,'https://example.test/news',self.window,5,kind='index',link_pattern=r'/press/device-\d+')
        self.assertEqual([p['url'] for p in links],['https://example.test/press/device-1'])

    def test_atom_uses_alternate_article_link_instead_of_self_metadata(self):
        xml=f'<feed xmlns="http://www.w3.org/2005/Atom"><entry><title>Release</title><link rel="self" href="https://example.test/api/1"/><link rel="alternate" href="{self.source["url"]}"/></entry></feed>'
        self.assertEqual(recent_links(xml,'https://example.test/feed',self.window,2)[0]['url'],self.source['url'])
