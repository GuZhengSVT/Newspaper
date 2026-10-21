"""Administrator-managed private accounts and durable, recipient-isolated delivery."""
from __future__ import annotations

import hashlib
import json
import re

from .notify import readiness, send_text, UncertainDelivery
from .storage import utcnow


def accounts(settings) -> list[dict]:
    raw = settings.values.get('_accounts', [])
    if not isinstance(raw, list):raise ValueError('accounts must be a list')
    sections = {s['id'] for s in settings.values.get('sections', [])}
    groups = {g['id']: set(g['sections']) for g in settings.values.get('editions', [])}
    defaults=settings.values.get('notifications',{}).get('defaults',{})
    if set(defaults)-{'subscriptions','bilingual'}:raise ValueError('Unknown account default')
    if type(defaults.get('bilingual',False)) is not bool:raise ValueError('Invalid default bilingual flag')
    default_targets=defaults.get('subscriptions',['*'])
    if not isinstance(default_targets,list) or any(not isinstance(t,str) for t in default_targets):
        raise ValueError('Invalid default subscriptions')
    if len(set(default_targets))!=len(default_targets) or set(default_targets)-(sections|set(groups)|{'*'}):
        raise ValueError('Unknown or duplicate default subscription')
    result = [];seen = set()
    for account in raw:
        if not isinstance(account, dict) or set(account) - {'id','name','enabled','emails','telegram_chat_id','subscriptions'}:
            raise ValueError('Unknown account setting')
        aid = account.get('id', '')
        if not isinstance(aid,str) or not re.fullmatch(r'[A-Za-z][A-Za-z0-9_-]{0,63}',aid) or aid in seen:
            raise ValueError('Invalid or duplicate account id')
        seen.add(aid)
        if type(account.get('enabled',True)) is not bool:raise ValueError('Invalid account enabled flag')
        emails = account.get('emails', [])
        if (not isinstance(emails,list) or len(emails)>50
            or any(not isinstance(e,str) or len(e)>254 or not re.fullmatch(r'[^@\s,<>;]+@[^@\s,<>;]+\.[^@\s,<>;]+',e) for e in emails)
            or len({e.casefold() for e in emails})!=len(emails)):
            raise ValueError('Invalid account email address')
        tg = account.get('telegram_chat_id', '')
        if not isinstance(tg,str) or (tg and not re.fullmatch(r'-?[0-9]{1,20}',tg)):
            raise ValueError('Telegram binding must be one numeric chat ID')
        subscriptions = account.get('subscriptions', [{'section':target} for target in default_targets])
        if not isinstance(subscriptions,list):raise ValueError('Invalid subscriptions')
        options = {};targets = set()
        # More specific rules override '*' and group rules, regardless of file order.
        rules = []
        for rule in subscriptions:
            if not isinstance(rule,dict) or set(rule)-{'section','bilingual'}:raise ValueError('Invalid subscription')
            target=rule.get('section')
            if not isinstance(target,str) or target not in sections | set(groups) | {'*'} or target in targets:
                raise ValueError('Unknown or duplicate subscription target')
            if type(rule.get('bilingual',defaults.get('bilingual',False))) is not bool:raise ValueError('Invalid bilingual subscription flag')
            targets.add(target);rules.append(rule)
        for rule in sorted(rules,key=lambda r: 0 if r['section']=='*' else 1 if r['section'] in groups else 2):
            target=rule['section'];ids=sections if target=='*' else groups.get(target,{target})
            for sid in ids:options[sid]=rule.get('bilingual',defaults.get('bilingual',False))
        if account.get('enabled',True):result.append({**account,'emails':emails,'telegram_chat_id':tg,'options':options})
    return result


def account_mode(settings) -> bool:
    return settings.values.get('notifications',{}).get('mode','legacy') == 'accounts'


def prepare_recipients(settings, store, batch) -> None:
    docs = [(key,store.document(key)) for key in json.loads(batch['expected'])]
    available = {s['id'] for _,doc in docs for s in doc['sections']}
    for account in accounts(settings):
        options = {sid:value for sid,value in account['options'].items() if sid in available}
        if not options:continue
        for channel, recipients in [('email',account['emails']), ('telegram',[account['telegram_chat_id']])]:
            if not settings.values.get('notifications',{}).get(channel,{}).get('enabled'):continue
            for recipient in recipients:
                if not recipient:continue
                store.db.execute('''insert or ignore into account_outbox
                    (batch_id,account_id,channel,recipient,options) values(?,?,?,?,?)''',
                    (batch['id'],account['id'],channel,recipient,json.dumps(options,sort_keys=True)))


def account_documents(store, batch, options) -> list:
    documents = []
    for key in json.loads(batch['expected']):
        doc = store.document(key)
        selected = [s for s in doc['sections'] if s['id'] in options]
        if selected:documents.append((key,{**doc,'sections':selected}))
    return documents


def deliver_accounts(settings, store) -> dict:
    from .batches import batch_subject
    from .messages import news_message
    result = {'sent':0,'failed':0,'unconfigured':0,'uncertain':0}
    if not account_mode(settings):return result
    now = utcnow().timestamp()
    interrupted = store.db.execute("update account_outbox set state='uncertain',last_error='InterruptedSend' where state='sending'")
    result['uncertain'] = interrupted.rowcount;store.db.commit()
    active = {a['id']:a for a in accounts(settings)}
    rows = store.db.execute('''select q.* from account_outbox q join notification_batches b on b.id=q.batch_id
        where q.state in ('pending','failed') and q.attempts<5 and q.next_attempt<=?
        and b.state='ready' and b.expires_at>?''',(now,now)).fetchall()
    for row in rows:
        aid=row['account_id'];channel=row['channel'];recipient=row['recipient']
        identity=(row['batch_id'],aid,channel,recipient)
        account=active.get(aid)
        # Revoked accounts, addresses or subscriptions must not receive delayed retries.
        destinations=account['emails'] if account and channel=='email' else [account['telegram_chat_id']] if account else []
        if account is None or recipient not in destinations:
            store.db.execute("update account_outbox set state='cancelled' where batch_id=? and account_id=? and channel=? and recipient=?",identity)
            store.db.commit();continue
        if not settings.values.get('notifications',{}).get(channel,{}).get('enabled'):continue
        if not readiness(channel,recipient=recipient):result['unconfigured']+=1;continue
        batch=store.db.execute('select * from notification_batches where id=?',(row['batch_id'],)).fetchone()
        if not all(store.published(key) for key in json.loads(batch['expected'])):continue
        options=json.loads(row['options'])
        if any(sid not in account['options'] or account['options'][sid]!=value for sid,value in options.items()):
            # Freeze content/progress for retries; a preference change cancels the old delivery.
            store.db.execute("update account_outbox set state='cancelled' where batch_id=? and account_id=? and channel=? and recipient=?",identity)
            store.db.commit();continue
        store.db.execute("update account_outbox set state='sending',attempts=attempts+1 where batch_id=? and account_id=? and channel=? and recipient=?",identity)
        store.db.commit()
        def checkpoint(part):
            store.db.execute('update account_outbox set progress=? where batch_id=? and account_id=? and channel=? and recipient=?',(part,*identity))
            store.db.commit()
        try:
            subject=batch_subject(settings,batch)
            message=news_message(settings,account_documents(store,batch,options),subject,
                                 notice=batch['correction_note'],bilingual_sections={sid for sid,v in options.items() if v})
            delivery_id=row['batch_id']+'-'+hashlib.sha256((aid+'\0'+channel+'\0'+recipient).encode()).hexdigest()[:24]
            send_text(settings,channel,subject,message['text'],delivery_id,recipient=recipient,
                      html_text=message['html'],telegram_parts=message['telegram'],
                      start_part=row['progress'],on_part_sent=checkpoint)
            state='sent';error=None;result['sent']+=1
        except UncertainDelivery:
            state='uncertain';error='UncertainDelivery';result['uncertain']+=1
        except Exception as exc:
            state='failed';error=type(exc).__name__;result['failed']+=1
        store.db.execute('''update account_outbox set state=?,last_error=?,next_attempt=?
            where batch_id=? and account_id=? and channel=? and recipient=?''',
            (state,error,now+min(3600,60*2**(row['attempts']+1)),*identity))
        store.db.commit()
    return result
