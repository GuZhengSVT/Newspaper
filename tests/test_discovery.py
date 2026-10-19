from __future__ import annotations

import copy
import json
import unittest
from unittest.mock import Mock, patch

import test_newspaper as fixtures
import test_editions as editions
from datetime import datetime
from newspaper.config import Settings
from newspaper.bounded import research
from newspaper.discovery import recent_links
from newspaper.pipeline import filter_invalid_articles, generate, restore_event_keys
from newspaper.research import fetch_source, publication_time


class DiscoveryTests(unittest.TestCase):
    setUp=fixtures.NewspaperTests.setUp
    tearDown=fixtures.NewspaperTests.tearDown

    def test_publisher_directory_discovers_new_article_without_search_indexing(self):
        page=f'''<a href="/about">About this publisher</a>
        <a href="/blog/old">2020-01-01 Outdated launch</a>
        <a href="/blog/new">{self.day} A synthetic important release</a>'''
        for content in [page,'<html><body>'+page+'</body></html>']:
            links=recent_links(content,'https://example.test/blog',self.window,3)
            self.assertEqual([s['url'] for s in links],['https://example.test/blog/new'])
            self.assertEqual(links[0]['published_at'],self.day)

    def test_rss_and_atom_dates_are_discovery_hints(self):
        for xml in [f'<rss><channel><item><title>Synthetic update</title><link>https://example.test/new</link><pubDate>{self.day}T10:00:00+08:00</pubDate></item></channel></rss>',
                    f'<feed xmlns="http://www.w3.org/2005/Atom"><entry><title>Synthetic update</title><link href="https://example.test/new"/><published>{self.day}T10:00:00+08:00</published></entry></feed>']:
            with self.subTest(xml=xml[:20]):
                links=recent_links(xml,'https://example.test/feed',self.window,3)
                self.assertEqual(links[0]['url'],'https://example.test/new')
                self.assertEqual(links[0]['published_at'],self.day)

    def test_publisher_timestamp_survives_html_script_removal(self):
        stamp=self.day+'T00:00:00+00:00'
        page='<script type="application/ld+json">'+json.dumps({'@graph':[
            {'@type':'Organization','datePublished':'2000-01-01T00:00:00Z'},
            {'@type':'BlogPosting','datePublished':stamp}]})+'</script>'
        page+='<article>'+'Synthetic evidence. '*15+'</article>'
        with patch('newspaper.research.fetch_page',return_value={'kind':'html','content':page}):
            source=fetch_source(self.settings,{'url':self.source['url'],'published_at':'2000-01-01'},self.root)
        self.assertEqual(source['published_at'],stamp)
        self.assertEqual(source['search_published_at'],'2000-01-01')
        self.assertIn('页面元数据 datePublished: '+stamp,source['content'])
        self.assertNotIn('Organization',source['content'])
        self.assertIsNone(publication_time('<script type="application/ld+json">{"@type":"Article","datePublished":"2026-10-19"}</script>'))

    def test_official_directory_candidate_enters_the_same_selection_and_fetch_flow(self):
        client=Mock();candidate={'event_key':'fixture|release|one','entities':['fixture'],'section_id':'fixture',
                                'importance':'重要','source_ids':['s2']}
        client.request.side_effect=[{'queries':[{'section_id':'fixture','query':'recent release'}]},
                                    {'candidates':[candidate]}]
        search=[{'section_id':'fixture','engines_ok':1,'sources':[
            {'url':'https://example.test/old','title':'Old homepage','snippet':'old','published_at':None}]}]
        index=[{'section_id':'fixture','engines_ok':1,'sources':[
            {'url':self.source['url'],'title':'New release','snippet':'Recent update','published_at':self.day}]}]
        with patch('newspaper.bounded.search',return_value=search), \
             patch('newspaper.bounded.discover',return_value=(index,{'discovery_links':1})), \
             patch('newspaper.bounded.fetch',return_value=[self.source]) as fetch:
            sources,candidates,_,_,metrics=research(self.settings,client,self.window,self.root,{})
        self.assertEqual(fetch.call_args.args[1][0]['url'],self.source['url'])
        self.assertEqual(sources[0]['id'],'s2')
        self.assertEqual(len(candidates),1)
        self.assertEqual(metrics['discovery_links'],1)

    def test_program_rejection_keeps_a_specific_diagnostic_without_full_draft(self):
        doc=copy.deepcopy(self.doc);diagnostics=[]
        article=doc['sections'][0]['articles'][0]
        article['facts'][0]['evidence'][0]['quote']='Quotation absent from source'
        result=filter_invalid_articles(doc,self.settings,[self.source],[],self.window,
                                       {article['event_key']},diagnostics=diagnostics)
        self.assertEqual(result['sections'][0]['articles'],[])
        self.assertEqual(diagnostics,[{'event_key':article['event_key'],
                                      'reason':'Evidence quotation was not found in the fetched source'}])
        self.assertNotIn('离线测试文本',json.dumps(diagnostics,ensure_ascii=False))

    def test_candidate_short_id_cannot_refer_to_another_section(self):
        doc=copy.deepcopy(self.doc);doc['sections'][0]['articles'][0]['candidate_id']='c1'
        candidates=[{'candidate_id':'c1','event_key':'stable|event|key','section_id':'other'}]
        with self.assertRaises(ValueError):restore_event_keys(doc,candidates)

    def test_format_retry_preserves_a_separate_semantic_repair_round(self):
        doc=editions.EditionTests.issue(self,'acgn');original=copy.deepcopy(doc)
        bad=copy.deepcopy(doc);bad['sections'][0]['id']='unconfigured'
        article=doc['sections'][0]['articles'][0];key=article['event_key']
        article.update(candidate_id='c1',event_key='model-reworded-key')
        candidate={'event_key':key,'entities':['Fixture Product'],'section_id':'fixture',
                   'importance':'重要','source_ids':['s1']}
        problem={'ok':False,'problems':['Make the conditional claim explicit'],
                 'affected_event_keys':[key],'article_reviews':[{'event_key':key,'ok':False,'problems':['Missing condition']}]}
        replacement=copy.deepcopy(original['sections'][0]['articles'][0]);replacement['event_key']='reworded-again'
        repair={'replacements':[{'section_id':'fixture','event_key':key,'article':replacement}],
                'summary':original['summary'],'highlights':original['highlights']}
        success={'ok':True,'summary_ok':True,'article_reviews':[{'event_key':key,'ok':True,'problems':[]}]}
        settings=Settings({**self.settings.values,'editions':[{'id':'acgn','name':'ACGN','sections':['fixture']}],
                           'search':{'mode':'bounded'}},self.settings.data,self.settings.web,self.settings.base_url)
        with patch('newspaper.pipeline.utcnow',return_value=datetime.fromisoformat(self.day+'T12:05:00+08:00')), \
             patch('newspaper.pipeline.bounded_research',return_value=([self.source],[candidate],[],[],{})), \
             patch('newspaper.pipeline.Model.request',side_effect=[bad,doc,problem,repair,success]) as model:
            result=generate(settings,self.store,notify=False)
        self.assertEqual(result['state'],'published')
        self.assertEqual([c.args[0] for c in model.call_args_list],['edit','edit','review','repair','review'])
        final=self.store.document(self.day+'/acgn')['sections'][0]['articles'][0]
        self.assertEqual(final['event_key'],key)
        self.assertNotIn('candidate_id',final)

    def test_rejected_empty_edition_is_not_published_as_a_no_news_success(self):
        doc=editions.EditionTests.issue(self,'acgn');doc['sections'][0]['articles']=[]
        doc['sections'][0]['empty_reason']='No verified articles'
        rejection={'ok':False,'summary_ok':False,'problems':['Insufficient current coverage'],'article_reviews':[]}
        settings=Settings({**self.settings.values,'editions':[{'id':'acgn','name':'ACGN','sections':['fixture']}],
                           'search':{'mode':'bounded'}},self.settings.data,self.settings.web,self.settings.base_url)
        with patch('newspaper.pipeline.utcnow',return_value=datetime.fromisoformat(self.day+'T12:05:00+08:00')), \
             patch('newspaper.pipeline.bounded_research',return_value=([self.source],[],[],[],{})), \
             patch('newspaper.pipeline.Model.request',side_effect=[copy.deepcopy(doc),rejection,copy.deepcopy(doc),rejection]):
            result=generate(settings,self.store,notify=False)
        self.assertEqual(result['state'],'partial_failure')
        self.assertFalse(self.store.published(self.day+'/acgn'))


if __name__=='__main__':unittest.main()
