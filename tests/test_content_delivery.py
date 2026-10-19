from __future__ import annotations

import copy
import json
import unittest
from datetime import datetime
from html import unescape
from unittest.mock import Mock, patch

import httpx

import test_newspaper as fixtures
import test_project as project
import test_editions as editions
from newspaper.config import Settings
from newspaper.batches import deliver_batches, finish_batches, prepare_batches
from newspaper.images import illustrate, source_images
from newspaper.messages import news_message, text_chunks, utf16_length
from newspaper.notify import send_text, UncertainDelivery
from newspaper.publish import build, markdown
from newspaper.validation import validate_report
from newspaper.schedule import jobs
from newspaper.pipeline import generate


class ContentTests(unittest.TestCase):
    setUp=fixtures.NewspaperTests.setUp
    tearDown=fixtures.NewspaperTests.tearDown

    def image(self):
        return {'url':'https://example.test/launch.jpg','caption':'合成测试发射图像',
                'source_id':'s1','source_url':self.source['url'],'width':1200,'height':800}

    def test_all_platforms_include_body_and_put_web_link_at_end(self):
        self.doc['sections'][0]['articles'][0]['limitations']='INTERNAL-NOTES'
        message=news_message(self.settings,[(self.day,self.doc)],'测试推送')
        self.assertIn('离线测试文本',message['text'])
        self.assertIn('离线测试文本',message['html'])
        self.assertIn('离线测试文本',''.join(p['text'] for p in message['telegram']))
        self.assertNotIn('INTERNAL-NOTES',str(message))
        url=self.settings.base_url+'/daily/'+self.day+'/'
        self.assertTrue(message['text'].endswith(url))
        self.assertTrue(message['telegram'][-1]['text'].endswith(url))
        self.assertGreater(message['html'].index(url),message['html'].index('离线测试文本'))

    def test_telegram_long_chinese_emoji_text_is_complete_and_safely_escaped(self):
        text='科学🔬与工程 <tag> & 原文。'*900
        self.doc['sections'][0]['articles'][0]['facts']=[{'text':text,'evidence':[]}]
        message=news_message(self.settings,[(self.day,self.doc)],'消息')
        parts=message['telegram']
        self.assertGreater(len(parts),2)
        for part in parts:
            self.assertLessEqual(utf16_length(unescape(part['text'])),4096)
            self.assertNotIn('<tag>',part['text'])
        joined=''.join(p['text'] for p in parts)
        self.assertEqual(joined.count('&lt;tag&gt;'),900)
        self.assertEqual(''.join(text_chunks(text)),text)

    def test_email_has_both_html_and_plain_text(self):
        message=news_message(self.settings,[(self.day,self.doc)],'Subject')
        smtp=Mock();smtp.send_message.return_value={}
        connection=Mock();connection.__enter__=Mock(return_value=smtp);connection.__exit__=Mock(return_value=False)
        env={k:'fixture' for k in ['SMTP_HOST','SMTP_USERNAME','SMTP_PASSWORD','EMAIL_FROM','EMAIL_TO']}
        env['SMTP_SECURITY']='ssl'
        with patch.dict('os.environ',env),patch('newspaper.notify.smtplib.SMTP_SSL',return_value=connection):
            send_text(self.settings,'email','Subject',message['text'],'fixture',html_text=message['html'])
        mail=smtp.send_message.call_args.args[0]
        self.assertEqual(mail.get_content_type(),'multipart/alternative')
        for body in mail.iter_parts():self.assertIn('离线测试文本',body.get_content())

    def test_image_source_binding_and_all_reading_formats(self):
        article=self.doc['sections'][0]['articles'][0];article['image']=self.image()
        self.source['images']=[{'url':article['image']['url'],'description':'Synthetic mission launch photograph'}]
        validate_report(self.doc,self.settings,[self.source],[],self.window)
        fixtures.NewspaperTests.add(self)
        build(self.settings,self.store)
        page=(self.settings.web/'current/daily'/self.day/'index.html').read_text()
        self.assertIn('class="story-image"',page)
        self.assertIn('loading="lazy"',page)
        self.assertIn('![合成测试发射图像]',markdown(self.doc))
        message=news_message(self.settings,[(self.day,self.doc)],'照片')
        self.assertIn('<img ',message['html'])
        self.assertEqual(len([p for p in message['telegram'] if p['method']=='sendPhoto']),1)
        article['image']['url']='https://example.test/unrelated.jpg'
        with self.assertRaisesRegex(ValueError,'cited article source'):
            validate_report(self.doc,self.settings,[self.source],[],self.window)

    def test_extraction_excludes_logos_unlabelled_and_non_https_images(self):
        content='''<header><img src="/header.jpg" alt="Synthetic mission launch photograph"></header>
        <article><img src="/launch.jpg" alt="Synthetic mission launch photograph">
        <img src="/logo.jpg" alt="Synthetic mission company logo">
        <img src="/unknown.jpg"><img src="data:image/png;base64,aaa" alt="Synthetic mission launch photograph"></article>'''
        images=source_images(content,'html',self.source['url'])
        self.assertEqual([i['url'] for i in images],['https://example.test/launch.jpg'])

    def test_optional_selection_cannot_borrow_another_event_or_invent_a_url(self):
        article=self.doc['sections'][0]['articles'][0]
        self.source['images']=[{'url':self.image()['url'],'description':'Synthetic mission launch photograph'}]
        client=Mock();client.request.return_value={'images':[
            {'event_key':'another-event','image_id':'i1','relevant':True,'caption':'不相关图像'},
            {'event_key':article['event_key'],'image_id':'invented','relevant':True,'caption':'编造的图像'}]}
        with patch('newspaper.images.verify_image') as verify:
            illustrate(self.doc,[self.source],client)
        verify.assert_not_called();self.assertNotIn('image',article)

    def test_optional_image_failure_does_not_discard_news(self):
        article=self.doc['sections'][0]['articles'][0]
        self.source['images']=[{'url':self.image()['url'],'description':'Synthetic mission launch photograph'}]
        client=Mock();client.request.return_value={'images':[{
            'event_key':article['event_key'],'image_id':'i1','relevant':True,'caption':'合成测试发射图像'}]}
        with patch('newspaper.images.verify_image',side_effect=ValueError('low quality')):
            illustrate(self.doc,[self.source],client)
        self.assertNotIn('image',article);self.assertEqual(article['facts'][0]['text'],'离线测试文本')
        with patch('newspaper.images.verify_image',return_value={'width':1200,'height':800}):
            illustrate(self.doc,[self.source],client)
        self.assertEqual(article['image'],self.image())

    def test_pipeline_adds_only_source_bound_image_after_fact_review(self):
        doc=editions.EditionTests.issue(self,'culture');article=doc['sections'][0]['articles'][0]
        article['candidate_id']='c1'
        self.source['images']=[{'url':self.image()['url'],'description':'Synthetic mission launch photograph'}]
        candidate={'event_key':article['event_key'],'entities':['Fixture Product'],'section_id':'fixture',
                   'importance':'重要','source_ids':['s1']}
        settings=Settings({**self.settings.values,'editions':[{'id':'culture','name':'Culture','sections':['fixture']}],
                           'search':{'mode':'bounded'}},self.settings.data,self.settings.web,self.settings.base_url)
        review={'ok':True,'summary_ok':True,'article_reviews':[{'event_key':article['event_key'],'ok':True,'problems':[]}]}
        selection={'images':[{'event_key':article['event_key'],'image_id':'i1','relevant':True,'caption':self.image()['caption']}]}
        with patch('newspaper.pipeline.utcnow',return_value=datetime.fromisoformat(self.day+'T12:05:00+08:00')), \
             patch('newspaper.pipeline.bounded_research',return_value=([self.source],[candidate],[],[],{})), \
             patch('newspaper.pipeline.Model.request',side_effect=[doc,review,selection]) as model, \
             patch('newspaper.images.verify_image',return_value={'width':1200,'height':800}):
            result=generate(settings,self.store,notify=False)
        self.assertEqual(result['state'],'published')
        self.assertEqual([c.args[0] for c in model.call_args_list],['edit','review','illustrate'])
        saved=self.store.document(self.day+'/culture')
        self.assertEqual(saved['sections'][0]['articles'][0]['image'],self.image())
        self.assertIn('![合成测试发射图像]',(self.settings.web/'current/downloads'/self.day/'culture.md').read_text())

    def test_empty_issue_review_receives_original_evidence_instead_of_an_empty_bundle(self):
        doc=editions.EditionTests.issue(self,'culture')
        doc['sections'][0].update(articles=[],empty_reason='原文复查后无合格候选')
        doc.update(summary='本期无合格新闻。',highlights=[])
        settings=Settings({**self.settings.values,'editions':[{'id':'culture','name':'Culture','sections':['fixture']}],
                           'search':{'mode':'bounded'}},self.settings.data,self.settings.web,self.settings.base_url)
        review={'ok':True,'summary_ok':True,'article_reviews':[]}
        with patch('newspaper.pipeline.utcnow',return_value=datetime.fromisoformat(self.day+'T12:05:00+08:00')), \
             patch('newspaper.pipeline.bounded_research',return_value=([self.source],[],[],[],{})), \
             patch('newspaper.pipeline.Model.request',side_effect=[doc,review]) as model:
            result=generate(settings,self.store,notify=False)
        self.assertEqual(result['state'],'published')
        for call in model.call_args_list:
            self.assertEqual(call.args[1]['sources'][0]['content'],self.source['content'])

    def test_empty_issue_retries_stale_candidate_review_without_restoring_filtered_articles(self):
        doc=editions.EditionTests.issue(self,'culture')
        old=copy.deepcopy(doc['sections'][0]['articles'][0])
        old['importance']='重要'
        doc['sections'][0]['articles']=[old]
        candidate={'event_key':old['event_key'],'entities':['Fixture Product'],'section_id':'fixture',
                   'importance':'重大','source_ids':['s1']}
        section={**self.settings.sections[0],'filter':{'importance':['重大']}}
        settings=Settings({**self.settings.values,'sections':[section],
            'editions':[{'id':'culture','name':'Culture','sections':['fixture']}],
            'search':{'mode':'bounded'}},self.settings.data,self.settings.web,self.settings.base_url)
        stale={'ok':True,'summary_ok':True,'article_reviews':[{'event_key':old['event_key'],'ok':True,'problems':[]}]}
        correct={'ok':True,'summary_ok':True,'article_reviews':[]}
        no_replacements={'replacements':[{'section_id':'fixture','event_key':old['event_key'],'article':None}],
                         'summary':'本期无合格新闻。','highlights':[]}
        with patch('newspaper.pipeline.utcnow',return_value=datetime.fromisoformat(self.day+'T12:05:00+08:00')), \
             patch('newspaper.pipeline.bounded_research',return_value=([self.source],[candidate],[],[],{})), \
             patch('newspaper.pipeline.Model.request',side_effect=[doc,no_replacements,stale,correct]) as model:
            result=generate(settings,self.store,notify=False)
        self.assertEqual(result['state'],'published')
        reviews=[c.args[1] for c in model.call_args_list if c.args[0]=='review']
        self.assertEqual(len(reviews),2)
        for payload in reviews:
            self.assertEqual(payload['expected_event_keys'],[])
            self.assertEqual(payload['candidates'],[])
            self.assertIn('sources',payload)
        self.assertIn('validation_error',reviews[1])
        self.assertEqual(self.store.document(self.day+'/culture')['sections'][0]['articles'],[])

    def test_unavailable_image_is_skipped_and_text_still_finishes(self):
        parts=[{'method':'sendMessage','text':'完整正文'},
               {'method':'sendPhoto','photo':'https://example.test/blocked.jpg'},
               {'method':'sendMessage','text':'末尾网页链接'}]
        responses=[httpx.Response(code,json={'ok':code==200},request=httpx.Request('POST','https://example.test')) for code in [200,400,200]]
        client=Mock();client.post.side_effect=responses;progress=[]
        with patch.dict('os.environ',{'TELEGRAM_BOT_TOKEN':'fixture','TELEGRAM_CHAT_ID':'fixture'}), \
             patch('newspaper.notify.httpx.Client') as factory,patch('newspaper.notify.time.sleep'):
            factory.return_value.__enter__.return_value=client
            send_text(self.settings,'telegram','News','Text','id',telegram_parts=parts,on_part_sent=progress.append)
        self.assertEqual(progress,[1,2,3]);self.assertEqual(client.post.call_count,3)

    def test_real_sender_resumes_after_rate_limit_without_repeating_first_message(self):
        parts=[{'method':'sendMessage','text':text,'parse_mode':'HTML'} for text in ['正文第一部分','正文第二部分','网页链接']]
        request=httpx.Request('POST','https://example.test')
        client=Mock();client.post.side_effect=[httpx.Response(200,json={'ok':True},request=request),
            httpx.Response(429,json={'ok':False},request=request)]
        progress=[]
        with patch.dict('os.environ',{'TELEGRAM_BOT_TOKEN':'fixture','TELEGRAM_CHAT_ID':'fixture'}), \
             patch('newspaper.notify.httpx.Client') as factory,patch('newspaper.notify.time.sleep'):
            factory.return_value.__enter__.return_value=client
            with self.assertRaises(httpx.HTTPStatusError):
                send_text(self.settings,'telegram','News','Text','id',telegram_parts=parts,on_part_sent=progress.append)
            self.assertEqual(progress,[1])
            client.post.side_effect=None;client.post.return_value=httpx.Response(200,json={'ok':True},request=request)
            client.post.reset_mock()
            send_text(self.settings,'telegram','News','Text','id',telegram_parts=parts,start_part=progress[-1],on_part_sent=progress.append)
        self.assertEqual([c.kwargs['json']['text'] for c in client.post.call_args_list],['正文第二部分','网页链接'])
        self.assertEqual(progress,[1,2,3])


class MultipartRetryTests(unittest.TestCase):
    setUp=project.ProjectTests.setUp
    tearDown=project.ProjectTests.tearDown
    save=project.ProjectTests.save

    def test_retry_resumes_after_last_acknowledged_part_after_restart(self):
        settings=self.make_settings();scheduled=jobs(settings,self.now)
        settings.values['notifications']['email']['enabled']=False
        ids=prepare_batches(settings,self.store,scheduled)
        for member in scheduled:self.save(*member)
        seen=[]
        def interrupted(*args,**kwargs):
            seen.append(kwargs['start_part']);kwargs['on_part_sent'](2)
            raise RuntimeError('Definite rejection of next part')
        with patch('newspaper.batches.readiness',return_value=True),patch('newspaper.batches.send_text',side_effect=interrupted):
            self.assertEqual(finish_batches(settings,self.store,ids)['failed'],1)
        self.store.close()
        from newspaper.storage import Store
        self.store=Store(settings)
        self.store.db.execute('update notification_outbox set next_attempt=0');self.store.db.commit()
        def resumed(*args,**kwargs):seen.append(kwargs['start_part'])
        with patch('newspaper.batches.readiness',return_value=True),patch('newspaper.batches.send_text',side_effect=resumed):
            self.assertEqual(deliver_batches(settings,self.store)['sent'],1)
            self.assertEqual(deliver_batches(settings,self.store)['sent'],0)
        self.assertEqual(seen,[0,2])

    def test_transport_timeout_is_not_automatically_repeated(self):
        client=Mock();client.post.side_effect=httpx.ReadTimeout('ambiguous')
        with patch.dict('os.environ',{'TELEGRAM_BOT_TOKEN':'fixture','TELEGRAM_CHAT_ID':'fixture'}), \
             patch('newspaper.notify.httpx.Client') as factory:
            factory.return_value.__enter__.return_value=client
            with self.assertRaises(UncertainDelivery):
                send_text(self.settings,'telegram','Subject','Body','fixture')
