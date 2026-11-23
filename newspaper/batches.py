"""Durable independent board notifications, isolated by board and schedule."""
from __future__ import annotations

import hashlib
import json

from .storage import expiry, utcnow
from .notify import readiness, send_text, UncertainDelivery
from .messages import news_message
from .publication import delivery_ready


def prepare_batches(settings, store, jobs: list) -> list[str]:
    from .accounts import account_mode
    tasks={}
    for job, window in jobs:
        if job.get('notify',True):tasks.setdefault((job.get('group_id',job['id']),job['task_id'],window['window_end']),[]).append((job,window))
    channels=[c for c in ['email','telegram'] if settings.values.get('notifications',{}).get(c,{}).get('enabled')]
    if channels and account_mode(settings):channels=['accounts']
    batch_id_for=lambda group_id,task_id,end: 'b-'+hashlib.sha256((group_id+'\0'+task_id+'\0'+end).encode()).hexdigest()[:32]
    desired={batch_id_for(*identity) for identity in tasks}
    scheduled_keys={w['date']+'/'+job['id'] for members in tasks.values() for job,w in members}
    backlog=set()
    for old in store.db.execute("select id,expected from notification_batches where state in ('waiting','superseded')").fetchall():
        expected=set(json.loads(old['expected']))
        if old['id'] not in desired and expected & scheduled_keys:
            # Waiting batches have never enqueued recipients. Split them without losing or duplicating delivery.
            backlog.update(expected)
            store.db.execute("update notification_batches set state='superseded' where id=?",(old['id'],))
    result=[]
    for (group_id,task_id,end), members in tasks.items():
        batch_id=batch_id_for(group_id,task_id,end)
        expected=[w['date']+'/'+job['id'] for job,w in members]
        existing=store.db.execute('select state from notification_batches where id=?',(batch_id,)).fetchone()
        # Never backfill notifications for previously published previews or migrated issues.
        if not existing and (not channels or (all(store.published(key) for key in expected) and not set(expected)&backlog)):continue
        if not existing:
            window=members[0][1];period=members[0][0]['schedule'].get('period','daily')
            store.db.execute('''insert into notification_batches
                (id,task_id,period,window_start,window_end,expected,channels,expires_at) values(?,?,?,?,?,?,?,?)''',
                (batch_id,task_id,period,window['window_start'],end,json.dumps(expected),json.dumps(channels),expiry(window['date'],settings.timezone)))
        elif existing['state']=='waiting':
            # Configuration can change while generation is incomplete. Sent batches stay immutable.
            store.db.execute('update notification_batches set expected=?,channels=? where id=?',(json.dumps(expected),json.dumps(channels),batch_id))
        result.append(batch_id)
    store.db.commit()
    return result


def prepare_waiting(settings, store, batch_ids=None):
    if batch_ids is None:
        batch_ids = [r[0] for r in store.db.execute(
            "SELECT id FROM notification_batches WHERE state='waiting' AND expires_at>?", (utcnow().timestamp(),))]
    for batch_id in batch_ids:
        row=store.db.execute('select * from notification_batches where id=?',(batch_id,)).fetchone()
        if row['state'] != 'waiting':
            continue
        expected = json.loads(row['expected'])
        if not all(store.published(key) for key in expected):
            continue
        eligible = [key for key in expected if not store.db.execute(
            'SELECT notify_suppressed FROM editions WHERE day=?', (key,)).fetchone()[0]]
        if not eligible:
            store.db.execute("UPDATE notification_batches SET state='cancelled' WHERE id=?", (batch_id,))
            continue
        if eligible != expected:
            store.db.execute('UPDATE notification_batches SET expected=? WHERE id=?', (json.dumps(eligible), batch_id))
            row = store.db.execute('SELECT * FROM notification_batches WHERE id=?', (batch_id,)).fetchone()
        if json.loads(row['channels'])==['accounts']:
            from .accounts import prepare_recipients
            prepare_recipients(settings,store,row)
        else:
            for channel in json.loads(row['channels']):
                store.db.execute('insert or ignore into notification_outbox(batch_id,channel) values(?,?)',(batch_id,channel))
        store.db.execute("update notification_batches set state='ready' where id=?",(batch_id,))
    store.db.commit()


def finish_batches(settings, store, batch_ids: list[str]) -> dict:
    prepare_waiting(settings, store, batch_ids)
    return deliver_batches(settings,store)


def batch_subject(settings, batch) -> str:
    label={'daily':'日报','weekly':'周报','monthly':'月报','interval':'定期新闻'}[batch['period']]
    subject=settings.values.get('project',{}).get('name','Newspaper')+' · '+label+' · '+batch['window_end'][:10]
    expected=json.loads(batch['expected'])
    if expected:
        slug=expected[0].split('/',1)[-1]
        group=next((g for g in sorted(settings.editions,key=lambda g:len(g['id']),reverse=True)
                    if slug==g['id'] or slug.startswith(g['id']+'-')),None)
        if group:subject+=' · '+group['name']
    if batch['correction_note']:subject+=' · 更正'
    return subject


def batch_message(settings, store, batch) -> tuple[str,dict]:
    subject=batch_subject(settings,batch)
    message=news_message(settings,[(key,store.document(key)) for key in json.loads(batch['expected'])],subject,
                         notice=batch['correction_note'])
    return subject,message


def deliver_batches(settings, store) -> dict:
    from .accounts import account_mode, deliver_accounts
    if account_mode(settings):return deliver_accounts(settings,store)
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
        keys = json.loads(batch['expected'])
        if not all(store.published(key) for key in keys) or not delivery_ready(settings, store, keys):continue
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
