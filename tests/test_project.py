from __future__ import annotations
import copy
import json
import subprocess
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch, Mock

import test_newspaper as fixtures
import test_editions as editions
from newspaper.config import Settings, ROOT
from newspaper.schedule import latest_window, jobs, task_identity
from newspaper.batches import prepare_batches, finish_batches, deliver_batches
from newspaper.notify import UncertainDelivery
from newspaper.notify import send_text
from newspaper.storage import Store, edition_key
from newspaper.discovery import discover
from newspaper.pipeline import generate
from newspaper.validation import validate_report
from newspaper.publish import build
from newspaper.bounded import SearchPlanError


class CalendarTests(unittest.TestCase):
    def test_manual_jobs_include_future_tasks_without_rewriting_the_regular_schedule(self):
        from tempfile import TemporaryDirectory
        with TemporaryDirectory() as directory:
            root=Path(directory)
            values={'project':{'timezone':'Asia/Shanghai'},'schedule':{'period':'daily','time':'12:00','start_date':'2026-10-20'},
                    'editions':[{'id':'first','name':'First','sections':['a','b']}],
                    'sections':[{'id':'a','name':'Daily'}, {'id':'b','name':'Weekly',
                    'schedule':{'period':'weekly','weekday':'mon','start_date':'2026-10-26'}}]}
            settings=Settings(values,root/'data',root/'web','https://example.test')
            now=datetime.fromisoformat('2026-10-20T02:30:00+08:00')
            self.assertEqual(jobs(settings,now),[])
            manual=jobs(settings,now,manual=True)
            self.assertEqual(len(manual),2)
            self.assertTrue(all('-manual-' in job['id'] and len(job['id'])<=40 for job,window in manual))
            self.assertTrue(all(window['window_end']==now.isoformat() for job,window in manual))
            self.assertEqual([window['window_start'] for job,window in manual],
                             ['2026-10-19T02:30:00+08:00','2026-10-13T02:30:00+08:00'])
            self.assertEqual(values['sections'][1]['schedule']['start_date'],'2026-10-26')

    def test_weekly_monday_has_full_previous_week_and_catches_up_on_tuesday(self):
        cfg={'period':'weekly','weekday':'mon','time':'09:00'}
        w=latest_window(datetime.fromisoformat('2026-10-20T10:00:00+08:00'),cfg,'Asia/Shanghai')
        self.assertEqual(w,{'date':'2026-10-19','window_start':'2026-10-12T09:00:00+08:00','window_end':'2026-10-19T09:00:00+08:00'})
        before=latest_window(datetime.fromisoformat('2026-10-19T08:59:00+08:00'),cfg,'Asia/Shanghai')
        self.assertEqual(before['date'],'2026-10-12')

    def test_monthly_short_month_and_interval_anchor(self):
        w=latest_window(datetime.fromisoformat('2027-02-28T12:00:00+00:00'),{'period':'monthly','month_day':31},'UTC')
        self.assertEqual(w['window_end'],'2027-02-28T12:00:00+00:00')
        self.assertEqual(w['window_start'],'2027-01-31T12:00:00+00:00')
        cfg={'period':'interval','every_days':3,'start_date':'2026-10-01','time':'10:00'}
        self.assertEqual(latest_window(datetime.fromisoformat('2026-10-08T11:00:00+00:00'),cfg,'UTC')['date'],'2026-10-07')
        self.assertIsNone(latest_window(datetime.fromisoformat('2026-09-30T11:00:00+00:00'),cfg,'UTC'))

    def test_weekly_window_preserves_wall_clock_across_dst(self):
        w=latest_window(datetime.fromisoformat('2026-11-02T12:00:00-05:00'),{'period':'weekly','weekday':'mon','time':'09:00'},'America/New_York')
        self.assertTrue(w['window_start'].endswith('-04:00'))
        self.assertTrue(w['window_end'].endswith('-05:00'))
        start,end=(datetime.fromisoformat(w[k]) for k in ['window_start','window_end'])
        self.assertEqual((end-start).total_seconds(),169*3600)

    def test_task_identity_distinguishes_period_weekday_time_and_start_date(self):
        base={'period':'weekly','weekday':'wed','time':'09:00'}
        ids=[task_identity(cfg,'UTC') for cfg in [base,{**base,'weekday':'fri'},{**base,'time':'12:00'},
             {'period':'daily','time':'09:00'},{'period':'monthly','time':'09:00'}, {**base,'start_date':'2026-10-01'}]]
        self.assertEqual(len(ids),len(set(ids)))
        self.assertEqual(task_identity(base,'UTC'),task_identity({**base,'time':'09:00:00'},'UTC'))


class ProjectTests(unittest.TestCase):
    setUp=fixtures.NewspaperTests.setUp
    tearDown=fixtures.NewspaperTests.tearDown

    def settings(self, schedules=None):
        sections=[{'id':'fixture','name':'First'},{'id':'other','name':'Second'}]
        if schedules:
            for section,schedule in zip(sections,schedules):section['schedule']=schedule
        return Settings({**self.settings_base,'project':{'name':'Test news','timezone':'Asia/Shanghai'},
                         'schedule':{'period':'daily','time':'12:00'},'sections':sections,
                         'editions':[{'id':'first','name':'First group','sections':['fixture']},
                                     {'id':'second','name':'Second group','sections':['other']}]},
                        self.root/'data',self.root/'web','https://example.test')

    def setUp(self):
        fixtures.NewspaperTests.setUp(self)
        self.settings_base=self.settings.values
        # Keep the existing fixtures' settings attribute; use a separate constructor name.
        self.make_settings=ProjectTests.settings.__get__(self)
        self.now=datetime.fromisoformat(self.day+'T12:05:00+08:00')

    def save(self, job, window):
        doc=editions.EditionTests.issue(self,job['id']);doc.update(window,title=job['name'])
        section=doc['sections'][0];section.update(id=job['sections'][0],name=job['name'])
        section['articles'][0]['event_key']='fixture|release|'+job['id']
        section['articles'][0]['published_at']=(datetime.fromisoformat(window['window_end'])-timedelta(hours=1)).isoformat()
        key=edition_key(doc);directory=self.root/'data'/'editions'/key;directory.mkdir(parents=True,exist_ok=True)
        (directory/'report.json').write_text(json.dumps(doc));(directory/'report.md').write_text('Fixture')
        self.store.add(doc,task_id=job['task_id']);self.store.db.execute("update editions set status='published' where day=?",(key,));self.store.db.commit()
        return doc

    def test_same_schedule_across_groups_sends_one_message_per_channel_without_repeats(self):
        settings=self.make_settings();scheduled=jobs(settings,self.now)
        batch_ids=prepare_batches(settings,self.store,scheduled)
        self.assertEqual(len(batch_ids),1)
        for job,window in scheduled:self.save(job,window)
        with patch('newspaper.batches.readiness',return_value=True),patch('newspaper.batches.send_text') as send:
            self.assertEqual(finish_batches(settings,self.store,batch_ids)['sent'],2)
            self.assertEqual(finish_batches(settings,self.store,batch_ids)['sent'],0)
            self.assertEqual(deliver_batches(settings,self.store)['sent'],0)
        self.assertEqual(send.call_count,2)
        for call in send.call_args_list:
            self.assertIn('First group',call.args[3]);self.assertIn('Second group',call.args[3])
        self.assertEqual(len({call.args[4] for call in send.call_args_list}),1)

    def test_different_schedule_tasks_are_not_batched_together(self):
        settings=self.make_settings([{'period':'weekly','weekday':'wed'},{'period':'weekly','weekday':'fri'}])
        scheduled=jobs(settings,self.now)
        self.assertNotEqual(scheduled[0][0]['task_id'],scheduled[1][0]['task_id'])
        ids=prepare_batches(settings,self.store,scheduled);self.assertEqual(len(ids),2)
        for job,window in scheduled:self.save(job,window)
        with patch('newspaper.batches.readiness',return_value=True),patch('newspaper.batches.send_text') as send:
            self.assertEqual(finish_batches(settings,self.store,ids)['sent'],4)
        self.assertEqual(len({c.args[4] for c in send.call_args_list}),2)
        for call in send.call_args_list:self.assertNotEqual('First group' in call.args[3],'Second group' in call.args[3])

    def test_daily_weekly_monthly_are_separate_even_at_the_same_clock_time(self):
        settings=self.make_settings()
        settings.values['sections'].append({'id':'third','name':'Third','schedule':{'period':'monthly'}})
        settings.values['editions'].append({'id':'third','name':'Third group','sections':['third']})
        settings.values['sections'][1]['schedule']={'period':'weekly','weekday':'mon'}
        scheduled=jobs(settings,self.now)
        ids=prepare_batches(settings,self.store,scheduled)
        self.assertEqual(len(ids),3)
        for member in scheduled:self.save(*member)
        with patch('newspaper.batches.readiness',return_value=True),patch('newspaper.batches.send_text') as send:
            self.assertEqual(finish_batches(settings,self.store,ids)['sent'],6)
            self.assertEqual(finish_batches(settings,self.store,ids)['sent'],0)
        self.assertEqual(len({c.args[4] for c in send.call_args_list}),3)

    def test_equivalent_schedule_defaults_share_a_publication(self):
        settings=self.make_settings()
        settings.values['editions']=[{'id':'first','name':'First','sections':['fixture','other']}]
        settings.values['sections'][1]['schedule']={'time':'12:00:00','weekday':'mon'}
        scheduled=jobs(settings,self.now)
        self.assertEqual(len(scheduled),1)
        self.assertEqual(scheduled[0][0]['id'],'first')
        self.assertEqual(len(scheduled[0][0]['sections']),2)

    def test_manual_task_selector_runs_only_the_selected_complete_task(self):
        settings=self.make_settings([{}, {'period':'weekly','weekday':'mon','start_date':'2026-10-26'}])
        selected=jobs(settings,self.now,manual=True)[1][0]['task_id']
        with patch('newspaper.pipeline.utcnow',return_value=self.now), \
             patch('newspaper.pipeline.generate_edition',return_value={'state':'published'}) as run:
            result=generate(settings,self.store,notify=False,manual=True,task_id=selected)
        self.assertEqual(result['state'],'published');self.assertEqual(run.call_count,1)
        job=run.call_args.args[2]
        self.assertEqual(job['sections'],['other']);self.assertIn('-manual-',job['id'])
        self.assertEqual(run.call_args.kwargs['publication_window']['window_end'],self.now.isoformat())

    def test_failed_plan_keeps_safe_contract_details_in_run_metrics(self):
        settings=self.make_settings();settings.values['search']={'mode':'bounded'}
        problems=[{'code':'section_limit','section_id':'fixture','count':3,'limit':2}]
        with patch('newspaper.pipeline.utcnow',return_value=self.now), \
             patch('newspaper.pipeline.bounded_research',side_effect=SearchPlanError(problems)):
            result=generate(settings,self.store,notify=False)
        self.assertEqual(result['state'],'partial_failure')
        rows=self.store.db.execute("select error,metrics from runs where status='failed'").fetchall()
        self.assertEqual(len(rows),2)
        for row in rows:
            self.assertEqual(row['error'],'SearchPlanError')
            self.assertEqual(json.loads(row['metrics'])['search_plan_errors'],problems)

    def test_partial_generation_waits_and_recovers_after_restart(self):
        settings=self.make_settings();scheduled=jobs(settings,self.now);ids=prepare_batches(settings,self.store,scheduled)
        self.save(*scheduled[0])
        with patch('newspaper.batches.send_text') as send:
            finish_batches(settings,self.store,ids);send.assert_not_called()
        self.store.close();self.store=Store(settings)
        self.assertEqual(prepare_batches(settings,self.store,scheduled),ids)
        self.save(*scheduled[1])
        with patch('newspaper.batches.readiness',return_value=True),patch('newspaper.batches.send_text') as send:
            self.assertEqual(finish_batches(settings,self.store,ids)['sent'],2)
        self.assertEqual(send.call_count,2)

    def test_only_failed_channel_retries(self):
        settings=self.make_settings();scheduled=jobs(settings,self.now);ids=prepare_batches(settings,self.store,scheduled)
        for member in scheduled:self.save(*member)
        with patch('newspaper.batches.readiness',return_value=True),patch('newspaper.batches.send_text',side_effect=[RuntimeError('rejected'),None,None]) as send:
            first=finish_batches(settings,self.store,ids)
            self.assertEqual((first['sent'],first['failed']),(1,1))
            self.store.db.execute('update notification_outbox set next_attempt=0');self.store.db.commit()
            self.assertEqual(deliver_batches(settings,self.store)['sent'],1)
        self.assertEqual(send.call_count,3)
        self.assertEqual([c.args[1] for c in send.call_args_list],['email','telegram','email'])

    def test_uncertain_network_delivery_and_interrupted_send_never_auto_repeat(self):
        settings=self.make_settings();scheduled=jobs(settings,self.now);ids=prepare_batches(settings,self.store,scheduled)
        for member in scheduled:self.save(*member)
        with patch('newspaper.batches.readiness',return_value=True),patch('newspaper.batches.send_text',side_effect=[UncertainDelivery(),None]) as send:
            self.assertEqual(finish_batches(settings,self.store,ids)['uncertain'],1)
            self.assertEqual(deliver_batches(settings,self.store)['sent'],0)
        self.assertEqual(send.call_count,2)
        self.store.db.execute("update notification_outbox set state='sending' where channel='telegram'");self.store.db.commit()
        with patch('newspaper.batches.send_text') as send:
            self.assertEqual(deliver_batches(settings,self.store)['uncertain'],1);send.assert_not_called()

    def test_smtp_quit_failure_after_acknowledged_submission_does_not_trigger_resend(self):
        import smtplib
        smtp=Mock();smtp.send_message.return_value={}
        connection=Mock();connection.__enter__=Mock(return_value=smtp)
        connection.__exit__=Mock(side_effect=smtplib.SMTPServerDisconnected('quit failed'))
        env={k:'fixture' for k in ['SMTP_HOST','SMTP_USERNAME','SMTP_PASSWORD','EMAIL_FROM','EMAIL_TO']}
        env['SMTP_SECURITY']='ssl'
        with patch.dict('os.environ',env),patch('newspaper.notify.smtplib.SMTP_SSL',return_value=connection):
            send_text(self.make_settings(),'email','Subject','Body','fixture-id')
        smtp.send_message.assert_called_once()

    def test_previously_published_migrated_issues_are_not_reannounced(self):
        settings=self.make_settings();scheduled=jobs(settings,self.now)
        for member in scheduled:self.save(*member)
        self.assertEqual(prepare_batches(settings,self.store,scheduled),[])

    def test_group_rename_reuses_published_slot_without_regeneration_or_resending(self):
        settings=self.make_settings();self.store.settings=settings
        scheduled=jobs(settings,self.now);ids=prepare_batches(settings,self.store,scheduled)
        for member in scheduled:self.save(*member)
        with patch('newspaper.batches.readiness',return_value=True),patch('newspaper.batches.send_text'):
            finish_batches(settings,self.store,ids)
        settings.values['editions'][0].update(id='renamed',name='Renamed group')
        with patch('newspaper.pipeline.utcnow',return_value=self.now),patch('newspaper.pipeline.Model.request') as model, \
             patch('newspaper.batches.send_text') as send:
            result=generate(settings,self.store)
        self.assertEqual(result['state'],'idle');model.assert_not_called();send.assert_not_called()
        self.assertEqual(result['editions'][0]['edition'],'first')
        self.assertFalse(self.store.published(self.day+'/renamed'))

    def test_publication_reuse_requires_the_same_sections_and_reporting_window(self):
        settings=self.make_settings();job,window=jobs(settings,self.now)[0];self.save(job,window)
        renamed={**job,'id':'renamed'}
        self.assertEqual(self.store.existing_publication(renamed,window),self.day+'/first')
        self.assertIsNone(self.store.existing_publication({**renamed,'sections':['other']},window))
        self.assertIsNone(self.store.existing_publication(renamed,{**window,'window_end':self.now.isoformat()}))

    def test_waiting_batch_tracks_current_members_instead_of_removed_publication_ids(self):
        settings=self.make_settings();scheduled=jobs(settings,self.now);ids=prepare_batches(settings,self.store,scheduled)
        settings.values['editions'][0]['id']='renamed'
        scheduled=jobs(settings,self.now)
        self.assertEqual(prepare_batches(settings,self.store,scheduled),ids)
        for member in scheduled:self.save(*member)
        with patch('newspaper.batches.readiness',return_value=True),patch('newspaper.batches.send_text') as send:
            self.assertEqual(finish_batches(settings,self.store,ids)['sent'],2)
        self.assertTrue(all('/renamed/' in call.args[3] for call in send.call_args_list))

    def test_correction_note_is_durable_in_both_channels_and_never_resent(self):
        settings=self.make_settings();scheduled=jobs(settings,self.now);ids=prepare_batches(settings,self.store,scheduled)
        for member in scheduled:self.save(*member)
        note='更正：此前推送为空，请以本次正文为准。'
        self.store.db.execute('update notification_batches set correction_note=? where id=?',(note,ids[0]));self.store.db.commit()
        self.store.close();self.store=Store(settings)
        with patch('newspaper.batches.readiness',return_value=True),patch('newspaper.batches.send_text') as send:
            self.assertEqual(finish_batches(settings,self.store,ids)['sent'],2)
            self.assertEqual(deliver_batches(settings,self.store)['sent'],0)
        for call in send.call_args_list:
            self.assertIn('更正',call.args[2]);self.assertIn(note,call.args[3])
            self.assertIn(note,call.kwargs['html_text'])
            self.assertIn(note,''.join(p['text'] for p in call.kwargs['telegram_parts']))
            self.assertIn('离线测试文本',call.args[3])

    def test_task_history_does_not_suppress_independent_weekly_or_monthly_reports(self):
        settings=self.make_settings();scheduled=jobs(settings,self.now)
        self.save(*scheduled[0]);task=scheduled[0][0]['task_id']
        self.assertEqual(len(self.store.history(task)),1)
        self.assertEqual(self.store.history('another-weekly-task'),[])
        self.assertEqual(self.store.related_history([{'entities':['Fixture Product']}],task_id='another-weekly-task'),[])

    def test_homepage_combines_latest_weekly_and_daily_content_in_configured_order(self):
        settings=self.make_settings([{}, {'period':'weekly','weekday':'mon'}]);self.store.settings=settings
        for member in jobs(settings,self.now):self.save(*member)
        build(settings,self.store)
        home=(settings.web/'current/index.html').read_text()
        self.assertIn('First group',home);self.assertIn('Second group',home)
        self.assertLess(home.index('First group'),home.index('Second group'))
        self.assertNotIn('尚未发布。',home)
        self.assertEqual(home.count('离线测试文本'),2)
        values={**settings.values,'homepage':{'mode':'groups','groups':['second']}}
        selected=Settings(values,settings.data,settings.web,settings.base_url);build(selected,self.store)
        home=(settings.web/'current/index.html').read_text()
        self.assertNotIn('First group',home);self.assertIn('Second group',home)
        self.assertIn('/feed-second.xml',str(list((settings.web/'current').glob('feed-*.xml'))))

    def test_homepage_index_does_not_render_bodies(self):
        settings=self.make_settings();self.store.settings=settings
        for member in jobs(settings,self.now):self.save(*member)
        index=Settings({**settings.values,'homepage':{'mode':'index'}},settings.data,settings.web,settings.base_url)
        build(index,self.store);home=(settings.web/'current/index.html').read_text()
        self.assertIn('First group',home);self.assertNotIn('离线测试文本',home)

    def test_homepage_layout_and_reader_switch_are_independent_of_content_mode(self):
        settings=self.make_settings();self.store.settings=settings
        for member in jobs(settings,self.now):self.save(*member)
        for mode in ['groups','sections','index']:
            for layout,switch in [('tabs',True),('columns',True),('tabs',False),('columns',False)]:
                with self.subTest(mode=mode,layout=layout,switch=switch):
                    configured=Settings({**settings.values,'homepage':{'mode':mode,'layout':layout,'layout_switch':switch}},
                                        settings.data,settings.web,settings.base_url)
                    configured.validate();build(configured,self.store)
                    home=(settings.web/'current/index.html').read_text()
                    self.assertIn('data-layout="'+layout+'"',home)
                    self.assertEqual('id="home-layout"' in home,switch)
                    self.assertIn('role="tablist"',home)
                    self.assertIn('aria-controls="'+('section-fixture' if mode=='sections' else 'column-first')+'"',home)
                    self.assertEqual('离线测试文本' in home,mode!='index')
        for home in [{'layout':'invalid'},{'layout_switch':'false'}]:
            with self.subTest(home=home),self.assertRaises(ValueError):
                Settings({**settings.values,'homepage':home},settings.data,settings.web,settings.base_url).validate()

    def test_politics_preset_has_eight_sections_and_valid_source_configuration(self):
        import tomllib
        project=tomllib.loads((ROOT/'examples/politics.toml').read_text())
        values={**project,'editions':project.pop('groups')}
        settings=Settings(values,self.root/'data',self.root/'web','https://example.test')
        settings.validate()
        self.assertEqual([s['name'] for s in settings.sections],['中国','美国','欧洲','俄乌战争','中东地区','南美','非洲','其他'])
        self.assertTrue(all(len(s['sources'])>=3 for s in settings.sections))

    def test_homepage_can_select_individual_sections(self):
        settings=self.make_settings();self.store.settings=settings
        for member in jobs(settings,self.now):self.save(*member)
        selected=Settings({**settings.values,'homepage':{'mode':'sections','sections':['other']}},settings.data,settings.web,settings.base_url)
        build(selected,self.store);home=(settings.web/'current/index.html').read_text()
        self.assertIn('Second',home);self.assertNotIn('First group',home)
        self.assertEqual(home.count('离线测试文本'),1)

    def test_prompt_customizations_are_scoped_to_the_requested_stage_and_sections(self):
        settings=self.make_settings()
        settings.values['prompts']={'plan':'Global plan','edit':'Global edit'}
        settings.values['sections'][0]['prompts']={'plan':'First plan','edit':'First edit'}
        settings.values['sections'][1]['prompts']={'plan':'Second plan'}
        text=settings.prompt_text('plan',[settings.sections[0]])
        self.assertIn('Global plan',text);self.assertIn('First plan',text)
        self.assertNotIn('Second plan',text);self.assertNotIn('First edit',text)

    def test_minute_polling_throttles_only_the_failed_publication(self):
        settings=self.make_settings();self.store.settings=settings
        scheduled=jobs(settings,self.now)
        now=self.now.timestamp()
        for offset in [600,500,400]:
            self.store.db.execute("insert into runs(day,started_at,finished_at,status) values(?,?,?,'failed')",
                                  (scheduled[0][1]['date']+'/'+scheduled[0][0]['id'],now-offset,now-offset))
        self.store.db.commit()
        self.save(*scheduled[1])
        with patch('newspaper.pipeline.utcnow',return_value=self.now),patch('newspaper.pipeline.Model.request') as model:
            result=generate(settings,self.store,notify=False)
        self.assertEqual(result['editions'][0]['state'],'retry_deferred')
        self.assertEqual(result['editions'][1]['state'],'already_published')
        model.assert_not_called()

    def test_private_project_file_is_loaded_and_validated_separately(self):
        (self.root/'project.local.toml').write_text((ROOT/'project.example.toml').read_text())
        runtime=(ROOT/'config.example.toml').read_text().replace('/var/lib/newspaper',str(self.root/'separate-data')).replace('/srv/newspaper',str(self.root/'separate-web'))
        path=self.root/'runtime.toml';path.write_text(runtime)
        with patch.dict('os.environ',{'NEWSPAPER_CREDENTIALS':str(self.root/'no-credentials')}):s=Settings.load(str(path))
        self.assertEqual([g['id'] for g in s.editions],['research','culture'])
        self.assertEqual(s.values['model']['id'],'YOUR_MODEL')
        self.assertEqual(s.values['sections'][1]['schedule']['weekday'],'mon')
        bad=Settings({**s.values,'homepage':{'groups':['unknown']}},s.data,s.web,s.base_url)
        with self.assertRaises(ValueError):bad.validate()

    def test_required_source_failure_is_not_an_empty_success(self):
        settings=self.make_settings();settings.values['sections'][0]['sources']=[{'url':'https://example.test/feed','kind':'rss','required':True}]
        with patch('newspaper.discovery.fetch_page',side_effect=subprocess.CalledProcessError(1,'fixture')):
            with self.assertRaisesRegex(ValueError,'Required sources'):discover(settings,self.window,self.root)
        with patch('newspaper.discovery.fetch_page',return_value={'kind':'html','content':'<rss><channel/></rss>'}):
            result,_=discover(settings,self.window,self.root)
        self.assertEqual(result[0]['index_status'],'no_recent_links')

    def test_section_filter_is_enforced_after_model_generation(self):
        doc=editions.EditionTests.issue(self,'first')
        settings=Settings({**self.settings.values,'sections':[{'id':'fixture','name':'测试板块','filter':{'reliability':['A'],'exclude_keywords':['blocked']}}]},self.settings.data,self.settings.web,self.settings.base_url)
        doc['sections'][0]['articles'][0]['title']='blocked announcement'
        with self.assertRaisesRegex(ValueError,'keyword filter'):validate_report(doc,settings,[self.source],[],self.window)

    def test_scheduled_pipeline_generates_then_sends_one_cross_group_batch_once(self):
        settings=self.make_settings();settings.values['search']={'mode':'bounded'};self.store.settings=settings
        scheduled=jobs(settings,self.now);responses=[];collections=[]
        for job,window in scheduled:
            doc=editions.EditionTests.issue(self,job['id']);doc.update(window,title=job['name'])
            section=next(s for s in settings.sections if s['id']==job['sections'][0])
            doc['sections'][0].update(id=section['id'],name=section['name'])
            article=doc['sections'][0]['articles'][0];article['event_key']='fixture|release|'+job['id'];article['candidate_id']='c1'
            candidate={'event_key':article['event_key'],'entities':['Fixture Product'],'section_id':section['id'],'importance':'重要','source_ids':['s1']}
            collections.append(([self.source],[candidate],[],[],{}))
            responses += [doc,{'ok':True,'summary_ok':True,'article_reviews':[{'event_key':article['event_key'],'ok':True,'problems':[]}]}]
        with patch('newspaper.pipeline.utcnow',return_value=self.now), \
             patch('newspaper.pipeline.bounded_research',side_effect=collections) as research, \
             patch('newspaper.pipeline.Model.request',side_effect=responses) as model, \
             patch('newspaper.batches.readiness',return_value=True),patch('newspaper.batches.send_text') as send:
            self.assertEqual(generate(settings,self.store)['state'],'published')
            self.assertEqual(generate(settings,self.store)['state'],'idle')
        self.assertEqual(research.call_count,2);self.assertEqual(model.call_count,4);self.assertEqual(send.call_count,2)
        self.assertEqual(self.store.db.execute('select count(*) from deliveries').fetchone()[0],0)
        self.assertEqual(self.store.db.execute("select count(*) from notification_outbox where state='sent'").fetchone()[0],2)

    def test_custom_fixed_queries_bypass_planner_and_keep_window(self):
        from newspaper.bounded import research
        settings=self.settings
        settings.values['sections'][0]['search']={'queries':['release after:{window_start}']}
        candidate={'event_key':'fixture|release|one','entities':['Fixture Product'],'section_id':'fixture','importance':'重要','source_ids':['s1']}
        client=Mock();client.request.return_value={'candidates':[candidate]}
        results=[{'section_id':'fixture','engines_ok':1,'sources':[{'url':self.source['url'],'title':'Fixture','snippet':'Preview'}]}]
        with patch('newspaper.bounded.search',return_value=results) as search, \
             patch('newspaper.bounded.discover',return_value=([],{})), \
             patch('newspaper.bounded.fetch',return_value=[self.source]):
            research(settings,client,self.window,self.root,{})
        self.assertEqual(client.request.call_args.args[0],'select')
        self.assertEqual(search.call_args.args[1][0]['query'],'release after:'+self.window['window_start'][:10])
        self.assertEqual(search.call_args.args[3],self.window)

    def test_section_body_limits_and_display_timezone_are_configurable(self):
        from newspaper.style import style_issues
        from newspaper.reading import report_view
        doc=editions.EditionTests.issue(self,'first');a=doc['sections'][0]['articles'][0]
        a['facts'][0]['text']='字'*700
        settings=Settings({**self.settings.values,'project':{'timezone':'UTC'},'sections':[
            {'id':'fixture','name':'测试板块','format':{'max_characters':1000}}]},self.settings.data,self.settings.web,self.settings.base_url)
        self.assertEqual(style_issues(doc,settings),[])
        self.assertTrue(style_issues(doc))
        self.assertEqual(report_view(doc,'fixture',settings)['sections'][0]['articles'][0]['time'][-5:],'02:00')
