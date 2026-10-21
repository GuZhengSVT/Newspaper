from __future__ import annotations

import copy
import json
import unittest
from datetime import datetime
from unittest.mock import Mock, patch

import test_newspaper as fixtures
import test_project as project
from newspaper.accounts import accounts
from newspaper.batches import prepare_batches, finish_batches, deliver_batches
from newspaper.config import Settings
from newspaper.messages import news_message
from newspaper.notify import UncertainDelivery, send_text, readiness
from newspaper.publish import build
from newspaper.schedule import jobs
from newspaper.storage import Store
from newspaper.translation import translate, sentence_units, validate_bilingual


class TranslationTests(unittest.TestCase):
    setUp=fixtures.NewspaperTests.setUp
    tearDown=fixtures.NewspaperTests.tearDown

    def bilingual(self, article=None):
        article=article or self.doc['sections'][0]['articles'][0]
        article['facts'][0]['text']='任务已经启动。结果仍需核实。'
        article['bilingual']={'title_en':'Synthetic mission','sentences':[
            {'en':'The mission has begun.','zh':'任务已经启动。'},
            {'en':'The results still need verification.','zh':'结果仍需核实。'}]}
        return article

    def test_pairs_keep_original_sentences_but_render_whole_paragraphs(self):
        article=self.bilingual()
        message=news_message(self.settings,[(self.day,self.doc)],'News',bilingual_sections={'fixture'})
        self.assertIn('The mission has begun. The results still need verification.\n\n任务已经启动。结果仍需核实。',message['text'])
        self.assertIn('<p lang="en"',message['html'])
        self.assertIn('任务已经启动。结果仍需核实。</p>',message['html'])
        message_cn=news_message(self.settings,[(self.day,self.doc)],'News')
        self.assertNotIn('The mission',str(message_cn))
        fixtures.NewspaperTests.add(self);build(self.settings,self.store)
        page=(self.settings.web/'current/daily'/self.day/'index.html').read_text()
        self.assertIn('data-section="fixture"',page)
        self.assertIn('The mission has begun. The results still need verification.</p><p lang="zh-CN">任务已经启动。结果仍需核实。',page)
        self.assertIn('bilingual-toggle',page)
        self.assertEqual(sentence_units(article),['任务已经启动。','结果仍需核实。'])

    def test_sentence_contract_retries_omissions_and_requires_independent_review(self):
        self.bilingual();article=self.doc['sections'][0]['articles'][0];article.pop('bilingual')
        client=Mock();client.request.side_effect=[
            {'title_en':'Mission','sentences':[{'id':0,'en':'The mission has begun.'}]},
            {'title_en':'Mission','sentences':[{'id':0,'en':'The mission has begun.'},{'id':1,'en':'The results still need verification.'}]},
            {'ok':True,'title_ok':True,'sentences':[{'id':0,'ok':True,'problems':[]},{'id':1,'ok':True,'problems':[]}]}]
        self.assertEqual(translate(self.doc,client),1)
        self.assertEqual([c.args[0] for c in client.request.call_args_list],['translate','translate','translate-review'])
        self.assertTrue(client.request.call_args_list[1].args[1]['feedback'])
        validate_bilingual(article,required=True)
        self.assertEqual(translate(self.doc,client),0)

    def test_failed_review_never_saves_unreviewed_translation(self):
        article=self.doc['sections'][0]['articles'][0]
        draft={'title_en':'Test','sentences':[{'id':0,'en':'Synthetic text.'}]}
        client=Mock();client.request.side_effect=[draft,{'ok':False,'problems':['Wrong meaning']},draft,{'ok':False}]
        with self.assertRaises(ValueError):translate(self.doc,client)
        self.assertNotIn('bilingual',article)

    def test_mutated_chinese_and_missing_english_are_rejected(self):
        article=self.bilingual()
        article['bilingual']['sentences'][1]['zh']='编造的中文。'
        with self.assertRaises(ValueError):validate_bilingual(article)
        article.pop('bilingual')
        with self.assertRaises(ValueError):
            news_message(self.settings,[(self.day,self.doc)],'News',bilingual_sections={'fixture'})

    def test_merged_fact_paragraphs_keep_sentence_order(self):
        article=self.bilingual()
        article['facts']=[{'text':str(i)+'号任务已启动。','evidence':[]} for i in range(7)]
        article['bilingual']['sentences']=[{'zh':str(i)+'号任务已启动。','en':f'Mission {i} has begun.'} for i in range(7)]
        from newspaper.reading import report_view
        view=report_view(self.doc,self.day,self.settings)
        pairs=view['sections'][0]['articles'][0]['bilingual_paragraphs']
        self.assertEqual(len(pairs),3)
        self.assertEqual(pairs[0]['en'],'Mission 0 has begun. Mission 1 has begun. Mission 2 has begun.')
        self.assertEqual(pairs[-1]['zh'],'6号任务已启动。')

    def test_production_translates_after_fact_review_and_saves_both_languages(self):
        from newspaper.pipeline import generate
        self.settings.values['translation']={'enabled':True}
        draft={'title_en':'Synthetic event','sentences':[{'id':0,'en':'Offline test text.'}]}
        review={'ok':True,'title_ok':True,'sentences':[{'id':0,'ok':True,'problems':[]}]}
        with patch('newspaper.pipeline.utcnow',return_value=datetime.fromisoformat(self.day+'T12:05:00+08:00')), \
             patch('newspaper.pipeline.gather',return_value={'sources':[self.source]}), \
             patch('newspaper.pipeline.Model.request',side_effect=[self.doc,{'ok':True},draft,review]) as model:
            self.assertEqual(generate(self.settings,self.store,notify=False)['state'],'published')
        self.assertEqual([c.args[0] for c in model.call_args_list],['edit','review','translate','translate-review'])
        self.assertIn('bilingual',self.store.document(self.day)['sections'][0]['articles'][0])

    def test_translation_failure_blocks_publication_and_delivery(self):
        from newspaper.pipeline import generate
        self.settings.values['translation']={'enabled':True}
        with patch('newspaper.pipeline.utcnow',return_value=datetime.fromisoformat(self.day+'T12:05:00+08:00')), \
             patch('newspaper.pipeline.gather',return_value={'sources':[self.source]}), \
             patch('newspaper.pipeline.Model.request',side_effect=[self.doc,{'ok':True},{},{}]):
            with self.assertRaises(ValueError):generate(self.settings,self.store,notify=True)
        self.assertFalse(self.store.published(self.day))
        self.assertEqual(self.store.db.execute('select count(*) from deliveries').fetchone()[0],0)


class AccountTests(unittest.TestCase):
    setUp=project.ProjectTests.setUp
    tearDown=project.ProjectTests.tearDown
    save=project.ProjectTests.save

    def setup_accounts(self):
        settings=self.make_settings()
        settings.values['translation']={'enabled':True}
        settings.values['notifications']['mode']='accounts'
        settings.values['_accounts']=[
            {'id':'Alice','emails':['a@example.test','a2@example.test'],'telegram_chat_id':'123',
             'subscriptions':[{'section':'first','bilingual':True}]},
            {'id':'Bob','emails':['b@example.test'],'subscriptions':[{'section':'other'}]},
        ]
        return settings

    def publish(self,settings):
        scheduled=jobs(settings,self.now);ids=prepare_batches(settings,self.store,scheduled)
        for job,window in scheduled:
            doc=self.save(job,window)
            for section in doc['sections']:
                for article in section['articles']:
                    article['bilingual']={'title_en':'Synthetic headline','sentences':[
                        {'zh':t,'en':'Synthetic translated sentence.'} for t in sentence_units(article)]}
            (settings.data/'editions'/window['date']/job['id']/'report.json').write_text(json.dumps(doc))
        return ids

    def test_defaults_and_specific_overrides_do_not_change_schedule(self):
        settings=self.setup_accounts()
        settings.values['notifications']['defaults']={'subscriptions':['*'],'bilingual':True}
        settings.values['_accounts']=[{'id':'Admin','emails':['admin@example.test']},
            {'id':'Custom','subscriptions':[{'section':'*'},{'section':'fixture','bilingual':False}]}]
        settings.validate()
        before=jobs(settings,self.now)
        values=accounts(settings)
        self.assertEqual(values[0]['options'],{'fixture':True,'other':True})
        self.assertEqual(values[1]['options'],{'fixture':False,'other':True})
        self.assertEqual(before,jobs(settings,self.now))
        settings.values['_accounts'][0]['subscriptions']=[]
        self.assertEqual(accounts(settings)[0]['options'],{})

    def test_each_recipient_gets_only_its_account_sections_and_language(self):
        settings=self.setup_accounts();ids=self.publish(settings)
        with patch('newspaper.accounts.readiness',return_value=True),patch('newspaper.accounts.send_text') as send:
            self.assertEqual(finish_batches(settings,self.store,ids)['sent'],4)
            self.assertEqual(deliver_batches(settings,self.store)['sent'],0)
        self.assertEqual(len({c.args[4] for c in send.call_args_list}),4)
        for call in send.call_args_list:
            recipient=call.kwargs['recipient'];text=call.args[3]
            if recipient=='b@example.test':
                self.assertIn('Second group',text);self.assertNotIn('First group',text)
                self.assertNotIn('Synthetic translated',text)
            else:
                self.assertIn('First group',text);self.assertNotIn('Second group',text)
                self.assertIn('Synthetic translated',text)
        self.assertEqual(self.store.db.execute('select count(*) from notification_outbox').fetchone()[0],0)

    def test_failures_restart_progress_and_ambiguous_send_are_isolated(self):
        settings=self.setup_accounts();ids=self.publish(settings)
        def fail(*args,**kw):
            if kw['recipient']=='123':kw['on_part_sent'](2);raise RuntimeError('Rejected next part')
            if kw['recipient']=='a2@example.test':raise UncertainDelivery()
        with patch('newspaper.accounts.readiness',return_value=True),patch('newspaper.accounts.send_text',side_effect=fail):
            result=finish_batches(settings,self.store,ids)
        self.assertEqual((result['sent'],result['failed'],result['uncertain']),(2,1,1))
        self.store.close();self.store=Store(settings)
        self.store.db.execute('update account_outbox set next_attempt=0');self.store.db.commit()
        with patch('newspaper.accounts.readiness',return_value=True),patch('newspaper.accounts.send_text') as send:
            self.assertEqual(deliver_batches(settings,self.store)['sent'],1)
            self.assertEqual(send.call_args.kwargs['recipient'],'123')
            self.assertEqual(send.call_args.kwargs['start_part'],2)

    def test_revoking_one_account_or_preference_cancels_delayed_delivery(self):
        settings=self.setup_accounts();ids=self.publish(settings)
        with patch('newspaper.accounts.readiness',return_value=False):finish_batches(settings,self.store,ids)
        settings.values['_accounts'][0]['enabled']=False
        settings.values['_accounts'][1]['subscriptions']=[]
        with patch('newspaper.accounts.readiness',return_value=True),patch('newspaper.accounts.send_text') as send:
            self.assertEqual(deliver_batches(settings,self.store)['sent'],0)
            send.assert_not_called()
        self.assertEqual(self.store.db.execute("select count(*) from account_outbox where state='cancelled'").fetchone()[0],4)

    def test_account_config_rejects_injection_duplicate_ids_and_unknown_topics(self):
        settings=self.setup_accounts()
        for change in [lambda a:a.update(emails=['a@example.test\nBcc: x@example.test']),
                       lambda a:a.update(telegram_chat_id='123,456'),
                       lambda a:a.update(subscriptions=[{'section':'unknown'}])]:
            bad=copy.deepcopy(settings.values);change(bad['_accounts'][0])
            with self.assertRaises(ValueError):Settings(bad,settings.data,settings.web,settings.base_url).validate()

    def test_explicit_recipient_does_not_require_or_use_global_destination(self):
        env={'SMTP_HOST':'fixture','SMTP_USERNAME':'fixture','SMTP_PASSWORD':'fixture','EMAIL_FROM':'bot@example.test','SMTP_SECURITY':'ssl'}
        smtp=Mock();smtp.send_message.return_value={}
        conn=Mock();conn.__enter__=Mock(return_value=smtp);conn.__exit__=Mock(return_value=False)
        with patch.dict('os.environ',env,clear=True),patch('newspaper.notify.smtplib.SMTP_SSL',return_value=conn):
            self.assertTrue(readiness('email',recipient='a@example.test'))
            send_text(self.settings,'email','News','Text','test',recipient='a@example.test')
        self.assertEqual(smtp.send_message.call_args.args[0]['To'],'a@example.test')
