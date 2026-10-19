"""Durable notification batches keyed by schedule and publication slot, not group."""
from __future__ import annotations

import hashlib
import json

from .storage import expiry, utcnow
from .notify import readiness, send_text, UncertainDelivery
from .messages import news_message


def prepare_batches(settings, store, jobs: list) -> list[str]:
    tasks={}
    for job, window in jobs:
        if job.get('notify',True):tasks.setdefault((job['task_id'],window['window_end']),[]).append((job,window))
    channels=[c for c in ['email','telegram'] if settings.values.get('notifications',{}).get(c,{}).get('enabled')]
    result=[]
    for (task_id,end), members in tasks.items():
        batch_id='b-'+hashlib.sha256((task_id+'\0'+end).encode()).hexdigest()[:32]
        expected=[w['date']+'/'+job['id'] for job,w in members]
        existing=store.db.execute('select 1 from notification_batches where id=?',(batch_id,)).fetchone()
        # Never backfill notifications for previously published previews or migrated issues.
        if not existing and (not channels or all(store.published(key) for key in expected)):continue
        if not existing:
            window=members[0][1];period=members[0][0]['schedule'].get('period','daily')
            store.db.execute('''insert into notification_batches
                (id,task_id,period,window_start,window_end,expected,channels,expires_at) values(?,?,?,?,?,?,?,?)''',
                (batch_id,task_id,period,window['window_start'],end,json.dumps(expected),json.dumps(channels),expiry(window['date'],settings.timezone)))
        result.append(batch_id)
    store.db.commit()
    return result


def finish_batches(settings, store, batch_ids: list[str]) -> dict:
    for batch_id in batch_ids:
        row=store.db.execute('select * from notification_batches where id=?',(batch_id,)).fetchone()
        if row['state']!='waiting' or not all(store.published(key) for key in json.loads(row['expected'])):continue
        for channel in json.loads(row['channels']):
            store.db.execute('insert or ignore into notification_outbox(batch_id,channel) values(?,?)',(batch_id,channel))
        store.db.execute("update notification_batches set state='ready' where id=?",(batch_id,))
    store.db.commit()
    return deliver_batches(settings,store)


def batch_message(settings, store, batch) -> tuple[str,dict]:
    label={'daily':'日报','weekly':'周报','monthly':'月报','interval':'定期新闻'}[batch['period']]
    subject=settings.values.get('project',{}).get('name','Newspaper')+' · '+label+' · '+batch['window_end'][:10]
    message=news_message(settings,[(key,store.document(key)) for key in json.loads(batch['expected'])],subject)
    return subject,message


def deliver_batches(settings, store) -> dict:
    now=utcnow().timestamp();result={'sent':0,'failed':0,'unconfigured':0,'uncertain':0}
    # A crash after provider acceptance cannot safely be retried (Telegram has no idempotency key).
    interrupted=store.db.execute("update notification_outbox set state='uncertain',last_error='InterruptedSend' where state='sending'")
    result['uncertain']+=interrupted.rowcount;store.db.commit()
    rows=store.db.execute('''select q.* from notification_outbox q join notification_batches b on b.id=q.batch_id
        where q.state in ('pending','failed') and q.attempts<5 and q.next_attempt<=?
        and b.state='ready' and b.expires_at>?''',(now,now)).fetchall()
    for row in rows:
        channel=row['channel']
        if not settings.values.get('notifications',{}).get(channel,{}).get('enabled'):continue
        if not readiness(channel):result['unconfigured']+=1;continue
        batch=store.db.execute('select * from notification_batches where id=?',(row['batch_id'],)).fetchone()
        if not all(store.published(key) for key in json.loads(batch['expected'])):continue
        subject,message=batch_message(settings,store,batch)
        store.db.execute("update notification_outbox set state='sending',attempts=attempts+1 where batch_id=? and channel=?",(row['batch_id'],channel))
        store.db.commit()
        def checkpoint(part):
            store.db.execute('update notification_outbox set progress=? where batch_id=? and channel=?',
                             (part,row['batch_id'],channel))
            store.db.commit()
        try:
            send_text(settings,channel,subject,message['text'],row['batch_id'],html_text=message['html'],
                      telegram_parts=message['telegram'],start_part=row['progress'],on_part_sent=checkpoint)
            state='sent';error=None;result['sent']+=1
        except UncertainDelivery:
            state='uncertain';error='UncertainDelivery';result['uncertain']+=1
        except Exception as exc:
            state='failed';error=type(exc).__name__;result['failed']+=1
        store.db.execute('''update notification_outbox set state=?,last_error=?,next_attempt=?
            where batch_id=? and channel=?''',(state,error,now+min(3600,60*2**(row['attempts']+1)),row['batch_id'],channel))
        store.db.commit()
    return result
