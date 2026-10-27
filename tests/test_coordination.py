"""Board isolation, effort routing and migration regressions; no external calls."""
import json
from unittest.mock import Mock, patch

import httpx

from fixtures import ProjectCase
from newspaper.batches import prepare_batches, finish_batches
from newspaper.coordinator import Coordinator
from newspaper.model import Model
from newspaper.pipeline import generate
from newspaper.schedule import jobs, scope_publication_history, task_identity
from newspaper.schedule import WEEKDAYS


class CoordinationTests(ProjectCase):
    def setUp(self):
        super().setUp()
        self.manager_start.stop();self.manager_approve.stop()

    def test_http_requests_use_max_manager_low_workers_and_preserve_cache_usage(self):
        settings=self.make_settings()
        settings.values['prompts']={'editorial':'LONG_EDITORIAL_BODY'*1000}
        response=httpx.Response(200,request=httpx.Request('POST','https://example.test/v1/chat/completions'),
            json={'choices':[{'finish_reason':'stop','message':{'content':'{}'}}],
                  'usage':{'prompt_cache_hit_tokens':256,'prompt_tokens_details':{'cached_tokens':0}}})
        client=Mock();client.post.return_value=response
        with patch.dict('os.environ',{'NEWSPAPER_API_KEY':'fixture'}),patch('newspaper.model.httpx.Client') as factory:
            factory.return_value.__enter__.return_value=client
            manager=Model(settings,role='manager',group_id='first')
            worker=Model(settings,group_id='first')
            manager.request('coordinate',{'phase':'delegate'})
            worker.request('edit',{'task':'first'})
            worker.request('translate',{'task':'second'})
            with self.assertRaisesRegex(ValueError,'compact summary budget'):
                manager.request('coordinate',{'body':'x'*16001})
        bodies=[c.kwargs['json'] for c in client.post.call_args_list]
        self.assertEqual([b['reasoning_effort'] for b in bodies],['max','low','low'])
        self.assertTrue(all(b['thinking']=={'type':'enabled'} and len(b['messages'])==2 for b in bodies))
        self.assertNotIn('LONG_EDITORIAL_BODY',bodies[0]['messages'][0]['content'])
        self.assertNotIn('first',bodies[2]['messages'][1]['content'])
        self.assertEqual(worker.usage[0]['tokens']['prompt_cache_hit_tokens'],256)
        self.assertEqual((manager.usage[0]['role'],worker.usage[0]['group_id']),('manager','first'))

    def test_manager_sees_only_compact_results_and_rejects_invented_articles(self):
        settings=self.make_settings();job,window=jobs(settings,self.now)[0]
        worker=Model(settings.for_edition(job),group_id=job['group_id'])
        manager=Coordinator(settings.for_edition(job),job,window,worker)
        doc=self.issue(job['id']);doc['sections'][0]['articles'][0]['facts'][0]['text']='PRIVATE_SOURCE_BODY'*5000
        key=doc['sections'][0]['articles'][0]['event_key']
        with patch.object(manager.model,'request',side_effect=[
            {'section_briefs':[{'section_id':'fixture','instruction':'Focus on verified releases.'}]},
            {'action':'publish','keep_event_keys':[key]}]) as request:
            manager.start();self.assertEqual(manager.approve(doc,{}),{key})
        self.assertIn('Focus on verified releases.',worker.delegation)
        encoded=json.dumps(request.call_args.args[1])
        self.assertNotIn('PRIVATE_SOURCE_BODY',encoded);self.assertNotIn('facts',request.call_args.args[1]['worker_results'][0]['articles'][0])
        self.assertIs(manager.model.usage,worker.usage)
        with patch.object(manager.model,'request') as request:
            self.assertIsNone(manager.approve(doc,{}));request.assert_not_called()
        manager.state={}
        with patch.object(manager.model,'request',return_value={'action':'publish','keep_event_keys':['unknown']}):
            with self.assertRaisesRegex(ValueError,'unknown articles'):manager.approve(doc,{})

    def test_real_coordinators_and_workers_complete_boards_serially_with_isolated_caches(self):
        settings=self.make_settings();settings.values['search']={'mode':'bounded'};self.store.settings=settings
        settings.values['sections'].append({'id':'weekly','name':'Weekly',
            'schedule':{'period':'weekly','weekday':WEEKDAYS[self.now.weekday()]}})
        settings.values['editions'][0]['sections'].append('weekly')
        timeline=[];caches=[];managers={}
        def request(model,stage,payload):
            timeline.append((model.group_id,model.role,stage))
            if stage=='coordinate':
                managers.setdefault(model.group_id,set()).add(id(model))
                if payload['phase']=='delegate':
                    return {'section_briefs':[{'section_id':s['id'],'instruction':'Use verified sources.'} for s in payload['sections']]}
                return {'action':'publish','keep_event_keys':[a['event_key'] for s in payload['worker_results'] for a in s['articles']]}
            if stage=='plan':return {}
            if stage=='review':return {'ok':True,'summary_ok':True,'article_reviews':[{'event_key':payload['report']['sections'][0]['articles'][0]['event_key'],'ok':True,'problems':[]}]}
            doc=self.issue(model.group_id);section=model.settings.sections[0]
            doc['sections'][0].update(id=section['id'],name=section['name'])
            doc['sections'][0]['articles'][0].update(event_key='fixture|release|'+model.group_id,candidate_id='c1')
            return doc
        def collect(scoped,worker,window,work,cache,**kwargs):
            if cache:self.assertEqual(cache,{'private':worker.group_id})
            caches.append(cache);cache['private']=worker.group_id
            worker.request('plan',{'sections':scoped.sections,'window':window})
            return [self.source],[{'event_key':'fixture|release|'+worker.group_id,'entities':['Fixture Product'],
                'section_id':scoped.sections[0]['id'],'importance':'重要','source_ids':['s1']}],[],[],{}
        def send(*args,**kwargs):timeline.append(('first' if 'First group' in args[3] else 'second','delivery',args[1]))
        with patch('newspaper.pipeline.utcnow',return_value=self.now), \
             patch.object(Model,'request',autospec=True,side_effect=request), \
             patch('newspaper.pipeline.bounded_research',side_effect=collect), \
             patch('newspaper.batches.readiness',return_value=True),patch('newspaper.batches.send_text',side_effect=send):
            result=generate(settings,self.store)
        self.assertEqual(result['state'],'published',result)
        self.assertIs(caches[0],caches[1]);self.assertIsNot(caches[0],caches[2])
        self.assertEqual({b:len(ids) for b,ids in managers.items()},{'first':1,'second':1})
        self.assertEqual([b for b,role,stage in timeline],['first']*14+['second']*7)
        for group in ['first','second']:
            self.assertEqual([(r,s) for b,r,s in timeline if b==group],[
                ('manager','coordinate'),('worker','plan'),('worker','edit'),('worker','review'),
                ('manager','coordinate'),('delivery','email'),('delivery','telegram')]*(2 if group=='first' else 1))

    def test_waiting_cross_board_batch_splits_without_losing_completed_publication(self):
        settings=self.make_settings();scheduled=jobs(settings,self.now)
        ids=prepare_batches(settings,self.store,scheduled)
        # Simulate an old cadence-wide waiting batch with one completed board.
        expected=[w['date']+'/'+j['id'] for j,w in scheduled]
        self.store.db.execute('delete from notification_batches where id=?',(ids[1],))
        self.store.db.execute('update notification_batches set id=?,expected=? where id=?',('legacy',json.dumps(expected),ids[0]))
        self.store.db.commit();self.save(*scheduled[0])
        ids=prepare_batches(settings,self.store,scheduled[:1])
        self.assertEqual(len(ids),1)
        self.assertEqual(self.store.db.execute("select state from notification_batches where id='legacy'").fetchone()[0],'superseded')
        with patch('newspaper.batches.readiness',return_value=True),patch('newspaper.batches.send_text') as send:
            self.assertEqual(finish_batches(settings,self.store,ids)['sent'],2)
            self.save(*scheduled[1])
            # A later run may select only the other already completed task.
            ids=prepare_batches(settings,self.store,scheduled[1:])
            self.assertEqual(len(ids),1)
            self.assertEqual(finish_batches(settings,self.store,ids)['sent'],2)
            self.assertEqual(finish_batches(settings,self.store,ids)['sent'],0)
        self.assertEqual(send.call_count,4)

    def test_history_migration_keeps_board_deduplication_without_cross_board_history(self):
        settings=self.make_settings();scheduled=jobs(settings,self.now)
        old_task=task_identity(scheduled[0][0]['schedule'],'Asia/Shanghai')
        for job,window in scheduled:
            self.save(job,window);self.store.set_task(window['date']+'/'+job['id'],old_task)
        scope_publication_history(settings,self.store)
        for job,window in scheduled:
            history=self.store.history(job['task_id'])
            self.assertEqual([h['id'] for h in history],['fixture|release|'+job['id']])
        self.assertEqual(prepare_batches(settings,self.store,scheduled),[])

    def test_coordinator_is_reused_for_windows_in_one_board_and_never_another_board(self):
        settings=self.make_settings();job,window=jobs(settings,self.now)[0]
        worker=Model(settings.for_edition(job),group_id='first')
        manager=Coordinator(settings.for_edition(job),job,window,worker)
        original=manager.model;manager.state={'approved':True}
        next_worker=Model(settings.for_edition(job),group_id='first')
        manager.bind(settings.for_edition(job),job,{**window,'date':'2026-10-28'},next_worker)
        self.assertIs(manager.model,original);self.assertIs(manager.model.usage,next_worker.usage)
        self.assertEqual(manager.state,{})
        other,other_window=jobs(settings,self.now)[1]
        with self.assertRaisesRegex(ValueError,'another board'):
            manager.bind(settings.for_edition(other),other,other_window,next_worker)
