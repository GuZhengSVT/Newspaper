from __future__ import annotations

import copy
import json
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

import test_newspaper as fixtures
from newspaper.config import Settings
from newspaper.evidence import selected_sources, used_evidence, validate_selection
from newspaper.pipeline import generate, apply_repair, keep_approved, filter_invalid_articles
from newspaper.publish import build, markdown
from newspaper.research import source_url, gather
from newspaper.retention import clean
from newspaper.storage import edition_key, expiry
from newspaper.validation import validate_report


class EditionTests(unittest.TestCase):
    setUp = fixtures.NewspaperTests.setUp
    tearDown = fixtures.NewspaperTests.tearDown

    def issue(self, slug):
        doc = copy.deepcopy(self.doc)
        doc.update(schema_version=2, edition=slug, title=slug)
        a = doc['sections'][0]['articles'][0]
        a.update(importance='重要', reliability='A', entities=['Fixture Product'], verification='official_statement')
        for f in a['facts']:f.update(kind='fact',verification='official_statement')
        return doc

    def save(self, doc):
        key = edition_key(doc)
        directory = self.settings.data / 'editions' / key
        directory.mkdir(parents=True)
        (directory / 'report.json').write_text(json.dumps(doc))
        (directory / 'report.md').write_text(markdown(doc))
        self.store.add(doc)
        self.store.db.execute("UPDATE editions SET status='published' WHERE day=?", (key,))
        self.store.db.commit()

    def test_same_date_two_issues_build_and_expire(self):
        for slug in ['research','culture']:
            self.save(self.issue(slug))
        settings = Settings({**self.settings.values, 'editions':[
            {'id':'research','name':'Research','sections':['fixture']},
            {'id':'culture','name':'Culture','sections':[]}]},self.settings.data,self.settings.web,self.settings.base_url)
        build(settings,self.store)
        for slug in ['research','culture']:
            page = (self.settings.web/'current/daily'/self.day/slug/'index.html').read_text()
            self.assertIn(f'/downloads/{self.day}/{slug}.md',page)
            self.assertTrue((self.settings.web/'current/downloads'/self.day/f'{slug}.md').is_file())
            self.assertIn(slug,(self.settings.web/f'current/feed-{slug}.xml').read_text())
        clean(settings,self.store,expiry(self.day))
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM event_entities').fetchone()[0],0)
        self.assertFalse((self.settings.data/'editions'/self.day).exists())

    def test_related_history_uses_normalized_entities(self):
        self.save(self.issue('culture'))
        self.assertEqual(len(self.store.related_history([{'entities':['Fixture-Product']} ])),1)
        self.assertEqual(self.store.related_history([{'entities':['Unrelated']}]),[])

    def two_edition_settings(self):
        values={**self.settings.values,
                'sections':self.settings.sections+[{'id':'culture-fixture','name':'Culture测试板块'}],
                'editions':[{'id':'research','name':'Research','sections':['fixture']},
                            {'id':'culture','name':'Culture','sections':['culture-fixture']}],
                'search':{'mode':'bounded'}}
        settings=Settings(values,self.settings.data,self.settings.web,self.settings.base_url)
        self.store.settings=settings
        return settings

    def two_edition_documents(self):
        tech=self.issue('research');culture=self.issue('culture')
        section=culture['sections'][0];section.update(id='culture-fixture',name='Culture测试板块')
        section['articles'][0]['event_key']='culture|synthetic|event'
        return [tech,culture]

    def test_two_disjoint_editions_build_with_whole_site_configuration(self):
        settings=self.two_edition_settings();documents=self.two_edition_documents()
        responses=[];collections=[]
        for doc in documents:
            a=doc['sections'][0]['articles'][0]
            responses.extend([doc,{'ok':True,'summary_ok':True,'article_reviews':[
                {'event_key':a['event_key'],'ok':True,'problems':[]}]}])
            candidate={'event_key':a['event_key'],'entities':a['entities'],'section_id':doc['sections'][0]['id'],
                       'importance':'重要','source_ids':['s1']}
            collections.append(([self.source],[candidate],[],[],{}))
        with patch('newspaper.pipeline.utcnow',return_value=datetime.fromisoformat(self.day+'T12:05:00+08:00')), \
             patch('newspaper.pipeline.bounded_research',side_effect=collections), \
             patch('newspaper.pipeline.Model.request',side_effect=responses):
            result=generate(settings,self.store,notify=False)
        self.assertEqual(result['state'],'published')
        for slug in ['research','culture']:
            self.assertTrue(self.store.published(self.day+'/'+slug))
            self.assertTrue((settings.web/'current/archive'/slug/'index.html').is_file())
            self.assertIn('/daily/'+self.day+'/'+slug+'/',(settings.web/f'current/feed-{slug}.xml').read_text())

    def test_two_ready_editions_resume_without_research_or_configuration_error(self):
        settings=self.two_edition_settings()
        for doc in self.two_edition_documents():
            self.save(doc);key=edition_key(doc)
            (settings.data/'editions'/key/'evidence.json').write_text(json.dumps({'sources':[self.source]}))
            self.store.db.execute("UPDATE editions SET status='ready' WHERE day=?",(key,))
        self.store.db.commit()
        with patch('newspaper.pipeline.utcnow',return_value=datetime.fromisoformat(self.day+'T12:05:00+08:00')), \
             patch('newspaper.pipeline.Model') as model:
            result=generate(settings,self.store,notify=False)
        self.assertEqual(result['state'],'published');model.assert_not_called()
        self.assertTrue(all(e['resumed'] for e in result['editions']))
        for slug in ['research','culture']:
            self.assertTrue(self.store.published(self.day+'/'+slug))

    def test_a_reliability_does_not_accept_unconfirmed_single_report(self):
        doc=self.issue('culture');doc['sections'][0]['articles'][0]['verification']='single_source'
        with self.assertRaises(ValueError):
            validate_report(doc,self.settings,[self.source],[],self.window)

    def test_core_claim_requires_its_own_corroboration(self):
        doc=self.issue('culture');doc['sections'][0]['articles'][0]['verification']='corroborated'
        doc['sections'][0]['articles'][0]['facts'][0]['verification']='corroborated'
        extra={**self.source,'id':'s2','url':'https://another.test/source'}
        doc['sources'].append({k:extra[k] for k in ['id','url','title','published_at']})
        with self.assertRaises(ValueError):
            validate_report(doc,self.settings,[self.source,extra],[],self.window)

    def test_empty_sections_hidden_from_web_and_markdown(self):
        doc=self.issue('culture');doc['sections'][0]['articles']=[];doc['sections'][0]['empty_reason']='No qualifying events'
        self.save(doc);build(self.settings,self.store)
        self.assertNotIn('## 测试板块',markdown(doc))
        page=(self.settings.web/'current/daily'/self.day/'culture/index.html').read_text()
        self.assertNotIn('测试板块',page)

    def test_preview_never_queues_or_sends_messages(self):
        collection={'section_id':'fixture','sources':[self.source],'notes':[]}
        moment=datetime.fromisoformat(self.day+'T12:05:00+08:00')
        with patch('newspaper.pipeline.utcnow',return_value=moment),patch('newspaper.pipeline.gather',return_value=collection), \
             patch('newspaper.pipeline.Model.request',side_effect=[copy.deepcopy(self.doc),{'ok':True}]), \
             patch('newspaper.pipeline.deliver') as sender:
            generate(self.settings,self.store,notify=False)
        sender.assert_not_called()
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM deliveries').fetchone()[0],0)
        directory=self.settings.data/'editions'/self.day
        self.assertTrue((directory/'evidence.json').is_file())
        self.assertFalse((directory/'raw.json').exists())

    def test_selection_cannot_invent_source_or_exceed_budget(self):
        candidate={'event_key':'e','entities':['thing'],'section_id':'fixture','importance':'重要','source_ids':['invented']}
        with self.assertRaises(ValueError):validate_selection({'candidates':[candidate]},self.settings,[self.source],2)
        candidate['source_ids']=['s1']
        with self.assertRaises(ValueError):validate_selection({'candidates':[candidate]},self.settings,[self.source],0)

    def test_evidence_bundle_keeps_quotes_drops_unrelated_pages(self):
        extra={**self.source,'id':'unused','content':'Unrelated article'}
        bundle=used_evidence(self.doc,[self.source,extra])
        self.assertEqual([s['id'] for s in bundle],['s1'])
        self.assertIn('This is synthetic evidence',bundle[0]['content'])

    def test_canonical_url_and_in_run_fetch_cache(self):
        self.assertEqual(source_url('https://EXAMPLE.test/source?utm_source=x#fragment'),'https://example.test/source')
        cache={};raw={'sources':[{'url':'https://example.test/source'}]}
        with patch('newspaper.research.collect',return_value=raw),patch('newspaper.research.fetch_source',return_value=self.source) as fetch:
            for i in range(2):gather(self.settings,{'id':'fixture'},self.window,self.root,cache=cache)
        self.assertEqual(fetch.call_count,1)

    def test_document_path_traversal_rejected(self):
        with self.assertRaises(ValueError):self.store.document(self.day+'/../../credentials')

    def test_edition_failure_does_not_block_other_edition(self):
        editions=[{'id':'research','name':'科技','sections':['fixture']},{'id':'culture','name':'Culture','sections':[]}]
        settings=Settings({**self.settings.values,'editions':editions},self.settings.data,self.settings.web,self.settings.base_url)
        with patch('newspaper.pipeline.utcnow',return_value=datetime.fromisoformat(self.day+'T12:05:00+08:00')), \
             patch('newspaper.pipeline.generate_edition',side_effect=[ValueError('failure'),{'state':'published'}]) as run:
            result=generate(settings,self.store,notify=False)
        self.assertEqual(run.call_count,2)
        self.assertEqual(result['state'],'partial_failure')

    def test_targeted_repair_does_not_change_unaffected_article(self):
        doc=self.issue('culture')
        original=copy.deepcopy(doc['sections'][0]['articles'][0])
        second={**copy.deepcopy(original),'event_key':'other|new|event','title':'Unchanged'}
        doc['sections'][0]['articles'].append(second)
        patch={'replacements':[{'section_id':'fixture','event_key':original['event_key'],'article':None}],
               'summary':'Updated summary','highlights':[]}
        result=apply_repair(doc,patch,{original['event_key']})
        self.assertEqual(result['sections'][0]['articles'],[second])
        self.assertEqual(len(doc['sections'][0]['articles']),2)
        with self.assertRaises(ValueError):apply_repair(doc,patch,{'unrelated'})

    def test_bounded_plan_must_cover_each_section(self):
        from newspaper.bounded import research
        from unittest.mock import Mock
        client=Mock();client.request.return_value={'queries':[]}
        with self.assertRaises(ValueError):research(self.settings,client,self.window,self.root,{})

    def test_bounded_research_fetches_only_selected_urls(self):
        from newspaper.bounded import research
        from unittest.mock import Mock
        import hashlib
        sid='s1'
        source={**self.source,'id':sid}
        client=Mock();client.request.side_effect=[{'queries':[{'section_id':'fixture','query':'current announcement'}]},
          {'candidates':[{'event_key':'fixture|release|one','entities':['fixture'],'section_id':'fixture','importance':'重要','source_ids':[sid]}]}]
        result=[{'section_id':'fixture','engines_ok':1,'sources':[
          {'url':self.source['url'],'title':'selected','snippet':'preview','published_at':None},
          {'url':'https://unused.test/source','title':'unused','snippet':'irrelevant','published_at':None}]}]
        with patch('newspaper.bounded.search',return_value=result),patch('newspaper.bounded.fetch',return_value=[source]) as fetch:
          sources,candidates,_,_,metrics=research(self.settings,client,self.window,self.root,{})
        self.assertEqual(len(fetch.call_args.args[1]),1)
        self.assertEqual(sources,[source]);self.assertEqual(metrics['plugin_queries'],1)

    def test_transient_gateway_overload_has_bounded_retry(self):
        from newspaper.model import Model
        import httpx,os
        request=httpx.Request('POST','https://example.test/chat/completions')
        bad=httpx.Response(503,json={'error':{'code':'system_memory_overloaded'}},request=request)
        good=httpx.Response(200,json={'choices':[{'finish_reason':'stop','message':{'content':'{"queries":[]}'}}],
                                    'usage':{'prompt_tokens':10,'completion_tokens':5}},request=request)
        with patch.dict(os.environ,{'NEWSPAPER_API_KEY':'synthetic-test-secret'}),patch('newspaper.model.httpx.Client') as client,              patch('newspaper.model.time.sleep') as sleep:
            client.return_value.__enter__.return_value.post.side_effect=[bad,good]
            model=Model(self.settings);self.assertEqual(model.request('plan',{}),{'queries':[]})
        sleep.assert_called_once_with(15)
        self.assertEqual(model.usage[0]['http_attempts'],2)
        self.assertNotIn('synthetic-test-secret',str(model.usage))

    def test_complete_v2_pipeline_publishes_without_push(self):
        doc=self.issue('culture')
        settings=Settings({**self.settings.values,'editions':[{'id':'culture','name':'Culture 日报','sections':['fixture']}],
                           'search':{'mode':'bounded'}},self.settings.data,self.settings.web,self.settings.base_url)
        candidate={'event_key':doc['sections'][0]['articles'][0]['event_key'],'entities':['Fixture Product'],
                   'section_id':'fixture','importance':'重要','source_ids':['s1']}
        moment=datetime.fromisoformat(self.day+'T12:05:00+08:00')
        with patch('newspaper.pipeline.utcnow',return_value=moment),              patch('newspaper.pipeline.bounded_research',return_value=([self.source],[candidate],[],[],{})),              patch('newspaper.pipeline.Model.request',side_effect=[doc,{'ok':True,'summary_ok':True,'article_reviews':[{'event_key':candidate['event_key'],'ok':True,'problems':[]}]}]),              patch('newspaper.pipeline.deliver') as sender:
            result=generate(settings,self.store,notify=False)
        self.assertEqual(result['state'],'published');sender.assert_not_called()
        key=self.day+'/culture'
        self.assertTrue(self.store.published(key))
        self.assertTrue((self.settings.web/'current/daily'/key/'index.html').is_file())
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM deliveries').fetchone()[0],0)

    def test_manual_cutoff_includes_verified_afternoon_news(self):
        cutoff=datetime.fromisoformat(self.day+'T21:03:11+08:00')
        doc=self.issue('culture');doc['window_end']=cutoff.isoformat()
        source={**self.source,'published_at':self.day+'T20:00:00+08:00'}
        article=doc['sections'][0]['articles'][0];article['published_at']=source['published_at']
        candidate={'event_key':article['event_key'],'entities':['Fixture Product'],'section_id':'fixture',
                   'importance':'重要','source_ids':['s1']}
        settings=Settings({**self.settings.values,'editions':[{'id':'culture','name':'Culture','sections':['fixture']}],
                           'search':{'mode':'bounded'}},self.settings.data,self.settings.web,self.settings.base_url)
        review={'ok':True,'summary_ok':True,'article_reviews':[{'event_key':article['event_key'],'ok':True,'problems':[]}]}
        with patch('newspaper.pipeline.utcnow',return_value=cutoff+timedelta(minutes=1)), \
             patch('newspaper.pipeline.bounded_research',return_value=([source],[candidate],[],[],{})) as research, \
             patch('newspaper.pipeline.Model.request',side_effect=[doc,review]):
            result=generate(settings,self.store,notify=False,cutoff=cutoff)
        self.assertEqual(result['state'],'published')
        final=self.store.document(self.day+'/culture')
        self.assertEqual(final['window_end'],cutoff.isoformat())
        self.assertEqual(final['window_start'],self.window['window_start'])
        self.assertEqual(research.call_args.args[2]['window_end'],cutoff.isoformat())

    def test_manual_cutoff_rejects_ambiguous_or_out_of_range_times(self):
        now=datetime.fromisoformat(self.day+'T21:05:00+08:00')
        values=[now.replace(tzinfo=None),now+timedelta(minutes=1),now-timedelta(days=1),now.replace(hour=11)]
        with patch('newspaper.pipeline.utcnow',return_value=now),patch('newspaper.pipeline.Model') as model:
            for cutoff in values:
                with self.subTest(cutoff=cutoff),self.assertRaises(ValueError):
                    generate(self.settings,self.store,cutoff=cutoff)
            model.assert_not_called()

    def test_general_importance_candidates_are_discarded_without_blocking_edition(self):
        candidate={'event_key':'e','entities':['thing'],'section_id':'fixture','importance':'一般','source_ids':['s1']}
        self.assertEqual(validate_selection({'candidates':[candidate]},self.settings,[self.source],2),[])

    def test_failed_rebuild_purges_unserved_release_indexes(self):
        stale=self.settings.web/'releases/stale'
        stale.mkdir();(stale/'feed.xml').write_text('expired news summary')
        with patch('newspaper.retention.build',side_effect=OSError('renderer unavailable')):
            with self.assertRaises(RuntimeError):clean(self.settings,self.store)
        self.assertFalse(stale.exists())

    def test_candidate_cap_prioritizes_major_events(self):
        base={'event_key':'e','entities':['thing'],'section_id':'fixture','importance':'重要','source_ids':['s1']}
        major={**base,'event_key':'major','importance':'重大'}
        result=validate_selection({'candidates':[base,major]},self.settings,[self.source],1)
        self.assertEqual(result,[major])

    def test_article_may_mix_official_and_corroborated_facts(self):
        doc=self.issue('culture');article=doc['sections'][0]['articles'][0]
        article['verification']='corroborated'
        extra={**self.source,'id':'s2','url':'https://another.test/source'}
        doc['sources'].append({k:extra[k] for k in ['id','url','title','published_at']})
        corroborated=copy.deepcopy(article['facts'][0]);corroborated['verification']='corroborated'
        corroborated['evidence'].append({'source_id':'s2','quote':'This is synthetic evidence for a test'})
        article['facts'].append(corroborated)
        validate_report(doc,self.settings,[self.source,extra],[],self.window)

    def test_unconfirmed_core_fact_cannot_hide_under_official_article_label(self):
        doc=self.issue('culture');doc['sections'][0]['articles'][0]['facts'][0]['verification']='single_source'
        with self.assertRaises(ValueError):validate_report(doc,self.settings,[self.source],[],self.window)

    def test_transport_restart_retry_stops_after_three_attempts(self):
        from newspaper.model import Model
        import httpx,os
        with patch.dict(os.environ,{'NEWSPAPER_API_KEY':'synthetic-test-secret'}),patch('newspaper.model.httpx.Client') as client,              patch('newspaper.model.time.sleep') as sleep:
            post=client.return_value.__enter__.return_value.post
            post.side_effect=httpx.RemoteProtocolError('upstream restarted')
            with self.assertRaises(httpx.RemoteProtocolError):Model(self.settings).request('plan',{})
        self.assertEqual(post.call_count,3)
        self.assertEqual([c.args[0] for c in sleep.call_args_list],[15,60])

    def test_invalid_credentials_are_not_retried_within_request(self):
        from newspaper.model import Model
        import httpx,os
        response=httpx.Response(401,request=httpx.Request('POST','https://example.test/v1'))
        with patch.dict(os.environ,{'NEWSPAPER_API_KEY':'synthetic-test-secret'}),patch('newspaper.model.httpx.Client') as client,              patch('newspaper.model.time.sleep') as sleep:
            post=client.return_value.__enter__.return_value.post;post.return_value=response
            with self.assertRaises(httpx.HTTPStatusError):Model(self.settings).request('plan',{})
        self.assertEqual(post.call_count,1);sleep.assert_not_called()

    def test_final_review_publishes_only_explicitly_approved_articles(self):
        doc=self.issue('culture');article=doc['sections'][0]['articles'][0]
        bad={**copy.deepcopy(article),'event_key':'rejected','title':'Unsupported'}
        doc['sections'][0]['articles'].append(bad)
        review={'article_reviews':[{'event_key':article['event_key'],'ok':True,'problems':[]},
                                  {'event_key':'rejected','ok':False,'problems':['Unverified date']}]}
        final=keep_approved(doc,review)
        self.assertEqual(final['sections'][0]['articles'],[article])
        self.assertNotIn('Unsupported',final['summary'])
        self.assertEqual(len(doc['sections'][0]['articles']),2)

    def test_incomplete_final_review_cannot_publish_partial_success(self):
        with self.assertRaises(ValueError):keep_approved(self.issue('culture'),{'article_reviews':[]})

    def test_legacy_and_new_edition_on_same_day_share_parent_directory(self):
        self.save(copy.deepcopy(self.doc));self.save(self.issue('culture'))
        build(self.settings,self.store)
        self.assertTrue((self.settings.web/'current/daily'/self.day/'index.html').exists())
        self.assertTrue((self.settings.web/'current/daily'/self.day/'culture/index.html').exists())

    def test_reviewed_ready_edition_resumes_without_calling_model_or_push(self):
        doc=self.issue('culture');self.save(doc)
        key=edition_key(doc)
        evidence={'sources':[self.source]}
        (self.settings.data/'editions'/key/'evidence.json').write_text(json.dumps(evidence))
        self.store.db.execute("UPDATE editions SET status='ready' WHERE day=?",(key,));self.store.db.commit()
        from newspaper.pipeline import generate_edition
        with patch('newspaper.pipeline.Model') as model,patch('newspaper.pipeline.deliver') as sender:
            result=generate_edition(self.settings,self.store,{'id':'culture'}, {},False)
        model.assert_not_called();sender.assert_not_called()
        self.assertTrue(result['resumed']);self.assertTrue(self.store.published(key))

    def test_invalid_quote_excludes_only_its_article_before_ai_review(self):
        doc=self.issue('culture');good=doc['sections'][0]['articles'][0]
        bad=copy.deepcopy(good);bad['event_key']='bad';bad['facts'][0]['evidence'][0]['quote']='Invented quotation not in source'
        doc['sections'][0]['articles'].append(bad)
        final=filter_invalid_articles(doc,self.settings,[self.source],[],self.window,{good['event_key'],'bad'})
        self.assertEqual(final['sections'][0]['articles'],[good])
        self.assertEqual(len(doc['sections'][0]['articles']),2)

    def test_global_structure_errors_are_not_hidden_by_article_filtering(self):
        doc=self.issue('culture');doc['sections'][0]['id']='unconfigured'
        with self.assertRaises(ValueError):filter_invalid_articles(doc,self.settings,[self.source],[],self.window,set())


if __name__=='__main__':unittest.main()
