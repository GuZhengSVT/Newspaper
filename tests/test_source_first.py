from __future__ import annotations

import json
import unittest
from unittest.mock import Mock, patch

from newspaper.config import Settings
from newspaper.bounded import research, plan_problems, SearchPlanError
from newspaper.discovery import discover, recent_links

from fixtures import NewspaperCase

class SourceFirstTests(NewspaperCase):

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

    def test_single_covered_section_gets_its_effective_budget_explicitly(self):
        client=Mock();client.request.side_effect=[{'queries':[]},{'candidates':[self.candidate()]}]
        with patch('newspaper.bounded.discover',return_value=([self.index()],{})), \
             patch('newspaper.bounded.fetch',return_value=[self.source]):
            research(self.settings,client,self.window,self.root,{})
        payload=client.request.call_args_list[0].args[1]
        self.assertEqual(payload['query_limit'],2)
        self.assertEqual(payload['section_query_limits'],{'fixture':2})
        self.assertEqual(payload['sections'][0]['search']['query_count'],2)
        self.assertNotIn('search',self.settings.sections[0])

    def test_oversubscribed_plan_is_corrected_once_before_any_search(self):
        queries=[{'section_id':'fixture','query':'missing topic '+str(i)} for i in range(3)]
        client=Mock();client.request.side_effect=[{'queries':queries},{'queries':queries[:2]},
                                                {'candidates':[self.candidate()]}]
        diagnostics={}
        with patch('newspaper.bounded.discover',return_value=([self.index()],{})) as discover, \
             patch('newspaper.bounded.search',return_value=[]) as search, \
             patch('newspaper.bounded.fetch',return_value=[self.source]):
            *_,metrics=research(self.settings,client,self.window,self.root,{},diagnostics)
        discover.assert_called_once();search.assert_called_once()
        self.assertEqual(search.call_args.args[1],queries[:2])
        self.assertEqual([c.args[0] for c in client.request.call_args_list],['plan','plan','select'])
        errors=client.request.call_args_list[1].args[1]['validation_errors']
        self.assertIn({'code':'section_limit','section_id':'fixture','count':3,'limit':2},errors)
        self.assertEqual(diagnostics['search_plan_checks'][-1]['problems'],[])
        self.assertEqual(metrics['plugin_queries'],2)

    def test_missing_mandatory_section_is_repaired_without_dropping_the_requirement(self):
        queries=[{'section_id':'fixture','query':'uncovered release'}]
        client=Mock();client.request.side_effect=[{'queries':[]},{'queries':queries},
                                                {'candidates':[self.candidate()]}]
        with patch('newspaper.bounded.discover',return_value=([self.index([],index_status='no_recent_links')],{})), \
             patch('newspaper.bounded.search',return_value=[self.index()]), \
             patch('newspaper.bounded.fetch',return_value=[self.source]):
            research(self.settings,client,self.window,self.root,{})
        corrected=client.request.call_args_list[1].args[1]
        self.assertEqual(corrected['mandatory_section_ids'],['fixture'])
        self.assertIn({'code':'missing_sections','section_ids':['fixture']},corrected['validation_errors'])

    def test_repeated_invalid_plan_stops_with_structured_diagnostics_and_no_search(self):
        client=Mock();client.request.return_value={'queries':[{'section_id':'fixture','query':'x'}]*3}
        diagnostics={}
        with patch('newspaper.bounded.discover',return_value=([self.index()],{})), \
             patch('newspaper.bounded.search') as search:
            with self.assertRaises(SearchPlanError) as raised:
                research(self.settings,client,self.window,self.root,{},diagnostics)
        self.assertEqual(client.request.call_count,2);search.assert_not_called()
        self.assertIn('section_limit',[p['code'] for p in raised.exception.problems])
        self.assertEqual(len(diagnostics['search_plan_checks']),2)

    def test_malformed_queries_and_shared_gap_limits_have_safe_diagnostics(self):
        sections={'first':{'id':'first'},'second':{'id':'second'}}
        malformed=plan_problems([None,{'section_id':['first'],'query':'private input'}],sections,{'first'},set(),3,3)
        self.assertEqual([p['code'] for p in malformed],['invalid_entry','unknown_section','missing_sections'])
        self.assertNotIn('private input',str(malformed))
        queries=[{'section_id':sid,'query':'topic'} for sid in ['first','first','second']]
        errors=plan_problems(queries,sections,set(),set(sections),2,1)
        self.assertEqual([p['code'] for p in errors],['total_limit','gap_limit'])

    def test_source_only_mode_avoids_both_planner_and_search_calls(self):
        self.settings.values['discovery']={'max_gap_queries':0}
        client=Mock();client.request.return_value={'candidates':[self.candidate()]}
        with patch('newspaper.bounded.discover',return_value=([self.index()],{})), \
             patch('newspaper.bounded.search') as search,patch('newspaper.bounded.fetch',return_value=[self.source]):
            *_,metrics=research(self.settings,client,self.window,self.root,{})
        self.assertEqual([c.args[0] for c in client.request.call_args_list],['select'])
        search.assert_not_called();self.assertEqual(metrics['plugin_queries'],0)

    def test_empty_preview_selection_is_rechecked_against_fetched_originals(self):
        client=Mock();client.request.side_effect=[{'queries':[]},{'candidates':[]},
                                                {'candidates':[self.candidate()]}]
        with patch('newspaper.bounded.discover',return_value=([self.index()],{})), \
             patch('newspaper.bounded.fetch',return_value=[self.source]) as fetch:
            sources,candidates,*_,metrics=research(self.settings,client,self.window,self.root,{})
        fetch.assert_called_once()
        self.assertEqual(candidates[0]['event_key'],self.candidate()['event_key'])
        self.assertTrue(metrics['empty_selection_probe'])
        payload=client.request.call_args_list[2].args[1]
        self.assertIn('fetched originals',payload['material_kind'])
        self.assertEqual(payload['sources'][0]['content'],self.source['content'])

    def test_verified_empty_selection_is_distinct_from_failed_source_fetches(self):
        client=Mock();client.request.side_effect=[{'queries':[]},{'candidates':[]},{'candidates':[]}]
        with patch('newspaper.bounded.discover',return_value=([self.index()],{})), \
             patch('newspaper.bounded.fetch',return_value=[self.source]):
            sources,candidates,*_,metrics=research(self.settings,client,self.window,self.root,{})
        self.assertEqual(len(sources),1);self.assertEqual(candidates,[])
        self.assertEqual(metrics['fetched_sources'],1)
        self.assertTrue(metrics['empty_selection_probe'])

    def test_failed_candidate_source_uses_remaining_budget_to_find_readable_evidence(self):
        alternative='https://publisher.test/original'
        index=self.index([{'url':self.source['url'],'title':'Synthetic release','snippet':'lead'},
                          {'url':alternative,'title':'Same synthetic release','snippet':'alternate lead'}])
        client=Mock();client.request.side_effect=[{'queries':[{'section_id':'fixture','query':'release'}]},
                                                {'candidates':[self.candidate()]}]
        recovered={**self.source,'url':alternative}
        with patch('newspaper.bounded.discover',return_value=([index],{})), \
             patch('newspaper.bounded.search',side_effect=[[],[index]]) as search, \
             patch('newspaper.bounded.fetch',side_effect=[[],[recovered]]) as fetch:
            sources,candidates,*_,metrics=research(self.settings,client,self.window,self.root,{})
        self.assertEqual(search.call_count,2)
        self.assertEqual(fetch.call_args_list[1].args[1][0]['url'],alternative)
        self.assertEqual(candidates[0]['source_ids'],['s2'])
        self.assertEqual(sources[0]['id'],'s2')
        self.assertEqual(metrics['source_recovery'][0]['fetched_sources'],1)
        self.assertEqual(metrics['plugin_queries'],2)

    def test_source_recovery_does_not_exceed_the_gap_query_budget(self):
        self.settings.values['discovery']={'max_gap_queries':1}
        client=Mock();client.request.side_effect=[{'queries':[{'section_id':'fixture','query':'release'}]},
                                                {'candidates':[self.candidate()]}]
        with patch('newspaper.bounded.discover',return_value=([self.index()],{})), \
             patch('newspaper.bounded.search',return_value=[]) as search, \
             patch('newspaper.bounded.fetch',return_value=[]):
            with self.assertRaisesRegex(ValueError,'No originals verified'):
                research(self.settings,client,self.window,self.root,{})
        search.assert_called_once()

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
