from __future__ import annotations

import copy
import json
import unittest
from unittest.mock import Mock, patch

from datetime import datetime
from newspaper.config import Settings
from newspaper.bounded import research
from newspaper.discovery import recent_links
from newspaper.pipeline import filter_invalid_articles, generate, restore_event_keys
from newspaper.research import fetch_source, publication_time, visible_publication_time
from newspaper.style import style_issues, compact_articles
from newspaper.reading import report_view

from fixtures import NewspaperCase

class DiscoveryTests(NewspaperCase):

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
                self.assertEqual(links[0]['publisher_published_at'],self.day+'T10:00:00+08:00')

    def test_numeric_publisher_paths_are_discovered(self):
        content=f'<a href="/{self.day[:4]}/{self.day[5:7]}/123456.html">A recent model release</a>'
        links=recent_links(content,'https://example.test/',self.window,3)
        self.assertEqual(len(links),1)

    def test_truncated_feed_is_a_failure_instead_of_an_empty_index(self):
        with self.assertRaisesRegex(ValueError,'truncated'):
            recent_links('<rss><channel><item><title>Broken','https://example.test/feed',self.window,3)

    def test_publisher_feed_time_is_kept_when_search_found_url_first(self):
        client=Mock();client.request.side_effect=[{'queries':[{'section_id':'fixture','query':'release'}]},
            {'candidates':[{'event_key':'fixture|release|one','entities':['Fixture'],'section_id':'fixture',
                            'importance':'重要','source_ids':['s1']}]}]
        item={'url':self.source['url'],'title':'Release','snippet':'Preview'}
        search=[{'section_id':'fixture','engines_ok':1,'sources':[item]}]
        index=[{'section_id':'fixture','engines_ok':1,'sources':[{**item,
            'discovery_index':'https://example.test/feed','publisher_published_at':self.source['published_at']}]}]
        with patch('newspaper.bounded.search',return_value=search), \
             patch('newspaper.bounded.discover',return_value=(index,{})), \
             patch('newspaper.bounded.fetch',return_value=[self.source]) as fetched:
            research(self.settings,client,self.window,self.root,{})
        self.assertEqual(fetched.call_args.args[1][0]['publisher_published_at'],self.source['published_at'])

    def test_empty_selection_without_originals_cannot_claim_no_news(self):
        client=Mock();client.request.side_effect=[{'queries':[{'section_id':'fixture','query':'release'}]},
                                                {'candidates':[]}]
        results=[{'section_id':'fixture','engines_ok':1,'sources':[
            {'url':self.source['url'],'title':'Search hit','snippet':'No verified original'}]}]
        diagnostics={}
        with patch('newspaper.bounded.search',return_value=results), \
             patch('newspaper.bounded.discover',return_value=([],{})), \
             patch('newspaper.bounded.fetch',return_value=[]):
            with self.assertRaisesRegex(ValueError,'No originals verified'):
                research(self.settings,client,self.window,self.root,{},diagnostics=diagnostics)
        self.assertEqual(diagnostics['result_urls'],1)
        self.assertEqual(diagnostics['selected_candidates'],0)

    def test_same_publisher_feed_can_supply_missing_timestamp(self):
        candidate={'url':self.source['url'],'discovery_index':'https://example.test/feed',
                   'publisher_published_at':self.source['published_at']}
        page={'kind':'html','content':'<article>'+'Synthetic release evidence. '*20+'</article>'}
        with patch('newspaper.research.fetch_page',return_value=page):
            source=fetch_source(self.settings,candidate,self.root)
        self.assertEqual(source['published_at'],self.source['published_at'])
        self.assertIn('发布者 RSS 发布时间',source['content'])
        candidate['discovery_index']='https://other.test/feed'
        with patch('newspaper.research.fetch_page',return_value=page):
            self.assertIsNone(fetch_source(self.settings,candidate,self.root)['published_at'])

    def test_displayed_clock_uses_only_an_explicit_publisher_timezone(self):
        text='Article title '+self.day.replace('-','/')+' 11:11:58 Published report. '
        self.assertEqual(visible_publication_time(text,'Asia/Shanghai'),self.day+'T11:11:58+08:00')
        self.assertIsNone(visible_publication_time(self.day+' Date only','Asia/Shanghai'))
        self.assertIsNone(visible_publication_time(self.day+' 25:11:58','Asia/Shanghai'))
        page={'kind':'html','content':'<article>'+text*10+'</article>'}
        with patch('newspaper.research.fetch_page',return_value=page):
            self.assertIsNone(fetch_source(self.settings,{'url':self.source['url']},self.root)['published_at'])
            configured=Settings({**self.settings.values,'publication_timezones':{'example.test':'Asia/Shanghai'}},
                                self.settings.data,self.settings.web,self.settings.base_url)
            source=fetch_source(configured,{'url':self.source['url']},self.root)
        self.assertEqual(source['published_at'],self.day+'T11:11:58+08:00')

    def test_malformed_model_json_has_one_bounded_format_retry(self):
        import httpx,os
        from newspaper.model import Model
        req=httpx.Request('POST','https://example.test/chat/completions')
        bad=httpx.Response(200,json={'choices':[{'finish_reason':'stop','message':{'content':'{"queries":['}}]},request=req)
        good=httpx.Response(200,json={'choices':[{'finish_reason':'stop','message':{'content':'{"queries":[]}'}}]},request=req)
        with patch.dict(os.environ,{'NEWSPAPER_API_KEY':'synthetic-test-secret'}),patch('newspaper.model.httpx.Client') as client:
            post=client.return_value.__enter__.return_value.post
            post.side_effect=[bad,good]
            model=Model(self.settings)
            self.assertEqual(model.request('plan',{}),{'queries':[]})
            self.assertEqual(post.call_count,2)
            self.assertTrue(model.usage[0]['format_error'])
            post.side_effect=[bad,bad]
            with self.assertRaises(ValueError):Model(self.settings).request('plan',{})
            self.assertEqual(post.call_count,4)

    def test_bad_quotation_gets_one_scoped_repair_before_exclusion(self):
        doc=self.issue('culture')
        article=doc['sections'][0]['articles'][0];key=article['event_key']
        article['candidate_id']='c1'
        bad=copy.deepcopy(doc);bad['sections'][0]['articles'][0]['facts'][0]['evidence'][0]['quote']='Absent quote'
        replacement=copy.deepcopy(article);replacement.pop('candidate_id')
        repair={'replacements':[{'section_id':'fixture','event_key':key,'article':replacement}],
                'summary':doc['summary'],'highlights':doc['highlights']}
        candidate={'event_key':key,'entities':['Fixture Product'],'section_id':'fixture',
                   'importance':'重要','source_ids':['s1']}
        success={'ok':True,'summary_ok':True,'article_reviews':[{'event_key':key,'ok':True,'problems':[]}]}
        settings=Settings({**self.settings.values,'editions':[{'id':'culture','name':'Culture','sections':['fixture']}],
                           'search':{'mode':'bounded'}},self.settings.data,self.settings.web,self.settings.base_url)
        with patch('newspaper.pipeline.utcnow',return_value=datetime.fromisoformat(self.day+'T12:05:00+08:00')), \
             patch('newspaper.pipeline.bounded_research',return_value=([self.source],[candidate],[],[],{})), \
             patch('newspaper.pipeline.Model.request',side_effect=[bad,repair,success]) as model:
            result=generate(settings,self.store,notify=False)
        self.assertEqual(result['state'],'published')
        self.assertEqual(result['editions'][0]['articles'],1)
        self.assertEqual([c.args[0] for c in model.call_args_list],['edit','repair','review'])

    def test_all_articles_needing_correction_receive_a_final_scoped_round(self):
        doc=self.issue('culture');article=doc['sections'][0]['articles'][0]
        key=article['event_key'];article['candidate_id']='c1'
        replacement=copy.deepcopy(article);replacement.pop('candidate_id')
        patch_doc={'replacements':[{'section_id':'fixture','event_key':key,'article':replacement}],
                   'summary':doc['summary'],'highlights':doc['highlights']}
        candidate={'event_key':key,'entities':['Fixture Product'],'section_id':'fixture',
                   'importance':'重要','source_ids':['s1']}
        correction={'ok':False,'problems':['Clarify attribution'],'affected_event_keys':[key],
                    'article_reviews':[{'event_key':key,'ok':False,'problems':['Clarify attribution']}]}
        success={'ok':True,'summary_ok':True,'article_reviews':[{'event_key':key,'ok':True,'problems':[]}]}
        settings=Settings({**self.settings.values,'editions':[{'id':'culture','name':'Culture','sections':['fixture']}],
                           'search':{'mode':'bounded'}},self.settings.data,self.settings.web,self.settings.base_url)
        with patch('newspaper.pipeline.utcnow',return_value=datetime.fromisoformat(self.day+'T12:05:00+08:00')), \
             patch('newspaper.pipeline.bounded_research',return_value=([self.source],[candidate],[],[],{})), \
             patch('newspaper.pipeline.Model.request',side_effect=[doc,correction,patch_doc,correction,patch_doc,success]) as model:
            result=generate(settings,self.store,notify=False)
        self.assertEqual(result['state'],'published')
        self.assertEqual(result['editions'][0]['articles'],1)
        self.assertEqual([c.args[0] for c in model.call_args_list],['edit','review','repair','review','repair','review'])

    def test_global_affected_list_cannot_rewrite_an_approved_article(self):
        doc=self.issue('culture');one=doc['sections'][0]['articles'][0]
        one['candidate_id']='c1';two=copy.deepcopy(one);two.update(event_key='fixture|second|event',candidate_id='c2')
        doc['sections'][0]['articles'].append(two);keys=[one['event_key'],two['event_key']]
        candidates=[{'event_key':k,'entities':['Fixture Product'],'section_id':'fixture','importance':'重要',
                     'source_ids':['s1']} for k in keys]
        correction={'ok':False,'problems':['Correct second article'],'affected_event_keys':keys,
                    'article_reviews':[{'event_key':keys[0],'ok':True,'problems':[]},
                                       {'event_key':keys[1],'ok':False,'problems':['Correct attribution']}]}
        replacement=copy.deepcopy(two);replacement.pop('candidate_id')
        patch_doc={'replacements':[{'section_id':'fixture','event_key':keys[1],'article':replacement}],
                   'summary':doc['summary'],'highlights':doc['highlights']}
        success={'ok':True,'summary_ok':True,'article_reviews':[{'event_key':k,'ok':True,'problems':[]} for k in keys]}
        settings=Settings({**self.settings.values,'editions':[{'id':'culture','name':'Culture','sections':['fixture']}],
                           'search':{'mode':'bounded'}},self.settings.data,self.settings.web,self.settings.base_url)
        with patch('newspaper.pipeline.utcnow',return_value=datetime.fromisoformat(self.day+'T12:05:00+08:00')), \
             patch('newspaper.pipeline.bounded_research',return_value=([self.source],candidates,[],[],{})), \
             patch('newspaper.pipeline.Model.request',side_effect=[doc,correction,patch_doc,success]) as model:
            result=generate(settings,self.store,notify=False)
        self.assertEqual(result['state'],'published')
        self.assertEqual(model.call_args_list[2].args[1]['event_keys'],[keys[1]])
        self.assertEqual(result['editions'][0]['articles'],2)

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
                                'importance':'重要','source_ids':['s1']}
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
        self.assertEqual(sources[0]['id'],'s1')
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
        doc=self.issue('culture');original=copy.deepcopy(doc)
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
        settings=Settings({**self.settings.values,'editions':[{'id':'culture','name':'Culture','sections':['fixture']}],
                           'search':{'mode':'bounded'}},self.settings.data,self.settings.web,self.settings.base_url)
        with patch('newspaper.pipeline.utcnow',return_value=datetime.fromisoformat(self.day+'T12:05:00+08:00')), \
             patch('newspaper.pipeline.bounded_research',return_value=([self.source],[candidate],[],[],{})), \
             patch('newspaper.pipeline.Model.request',side_effect=[bad,doc,problem,repair,success]) as model:
            result=generate(settings,self.store,notify=False)
        self.assertEqual(result['state'],'published')
        self.assertEqual([c.args[0] for c in model.call_args_list],['edit','edit','review','repair','review'])
        final=self.store.document(self.day+'/culture')['sections'][0]['articles'][0]
        self.assertEqual(final['event_key'],key)
        self.assertNotIn('candidate_id',final)

    def test_rejected_empty_edition_is_not_published_as_a_no_news_success(self):
        doc=self.issue('culture');doc['sections'][0]['articles']=[]
        doc['sections'][0]['empty_reason']='No verified articles'
        rejection={'ok':False,'summary_ok':False,'problems':['Insufficient current coverage'],'article_reviews':[]}
        settings=Settings({**self.settings.values,'editions':[{'id':'culture','name':'Culture','sections':['fixture']}],
                           'search':{'mode':'bounded'}},self.settings.data,self.settings.web,self.settings.base_url)
        with patch('newspaper.pipeline.utcnow',return_value=datetime.fromisoformat(self.day+'T12:05:00+08:00')), \
             patch('newspaper.pipeline.bounded_research',return_value=([self.source],[],[],[],{})), \
             patch('newspaper.pipeline.Model.request',side_effect=[copy.deepcopy(doc),rejection,copy.deepcopy(doc),rejection]):
            result=generate(settings,self.store,notify=False)
        self.assertEqual(result['state'],'partial_failure')
        self.assertFalse(self.store.published(self.day+'/culture'))

    def test_short_claims_are_grouped_for_reading_instead_of_excluding_the_news(self):
        doc=self.issue('culture');article=doc['sections'][0]['articles'][0]
        article['facts']=[{**copy.deepcopy(article['facts'][0]),'text':f'合成测试事实{i}。'} for i in range(7)]
        self.assertEqual(style_issues(doc),[])
        view=report_view(doc,self.day+'/culture')['sections'][0]['articles'][0]
        self.assertLessEqual(len(view['paragraphs']),3)
        for i in range(7):self.assertIn(f'合成测试事实{i}。',''.join(view['paragraphs']))

    def test_compaction_cannot_replace_evidence_or_verification(self):
        doc=self.issue('culture');article=doc['sections'][0]['articles'][0]
        original=copy.deepcopy(doc)
        patch={'articles':[{'event_key':article['event_key'],'facts':[{
            'index':0,'text':'简洁的合成事实','verification':'single_source','evidence':[]}]}]}
        result=compact_articles(doc,patch,{article['event_key']})
        fact=result['sections'][0]['articles'][0]['facts'][0]
        self.assertEqual(fact['evidence'],article['facts'][0]['evidence'])
        self.assertEqual(fact['verification'],'official_statement')
        self.assertEqual(doc,original)
        patch['articles'][0]['facts'][0]['index']=99
        with self.assertRaises(ValueError):compact_articles(doc,patch,{article['event_key']})


if __name__=='__main__':unittest.main()
