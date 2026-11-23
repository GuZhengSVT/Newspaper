"""Private, deduplicated failure alerts for one explicitly configured account."""
from __future__ import annotations

import hashlib
import json

from .accounts import accounts
from .notify import UncertainDelivery, readiness, send_text
from .storage import utcnow


COMMANDS = {'run', 'notify', 'build', 'clean', 'translate', 'import-legacy'}


def record_command(settings, store, command: str, error: Exception | None = None) -> None:
    if not settings.values.get('notifications', {}).get('failure_alerts'):return
    if error is None:
        store.db.execute('delete from workflow_health where command=?', (command,))
    else:
        now = utcnow().timestamp()
        # Exception text from providers can expose tokens, so retain only the type.
        store.db.execute('''insert into workflow_health values(?,1,?,?,?)
            on conflict(command) do update set failures=failures+1,
            updated_at=excluded.updated_at,last_error=excluded.last_error''',
            (command, now, now, type(error).__name__))
    store.db.commit()


def incidents(store, now: float) -> list[tuple[str, str]]:
    result = []
    if store.db.execute("SELECT 1 FROM sqlite_master WHERE name='model_backoff'").fetchone():
        for row in store.db.execute('SELECT * FROM model_backoff WHERE failures>=3'):
            incident = f"model:{row['provider']}:{row['first_failure_at']}"
            body = (f"模型供应商连接：{row['provider']}\n连续请求失败 {row['failures']} 次，已进入退避。\n"
                    f"最近错误：{row['error_type']}。")
            result.append((incident, body))
    failed = store.db.execute('''select day,count(*) as failures from runs
        where status='failed' and started_at>? group by day having count(*)>=3''',
        (now - 6 * 3600,)).fetchall()
    for group in failed:
        key = group['day']
        if store.published(key):continue
        row = store.db.execute('select * from runs where day=? order by id desc limit 1', (key,)).fetchone()
        if row['status'] != 'failed':continue
        metrics = json.loads(row['metrics'])
        body = (f"发布任务：{key}\n6 小时内已失败 {group['failures']} 次，达到自动重试上限。\n"
                f"最后运行：{row['id']}\n阶段：{metrics.get('stage', 'unknown')}\n错误：{row['error']}。")
        reasons = metrics.get('translation_failures', [])
        if reasons:
            failure = reasons[-1]
            body += f"\n文章：{failure.get('event_key', '')}\n原因：{failure.get('reason', '')}"
            review = failure.get('review', {})
            problems = review.get('problems', []) + [
                p for sentence in review.get('sentence_verdicts', []) for p in sentence.get('problems', [])]
            body += ''.join('\n- ' + str(p)[:400] for p in list(dict.fromkeys(problems))[:5])
        elif metrics.get('validation_errors'):
            body += '\n原因：' + str(metrics['validation_errors'][-1])[:600]
        result.append(('publication:' + key, body))
    for table, key_fields in [('account_outbox', ('batch_id', 'account_id', 'channel', 'recipient')),
                              ('notification_outbox', ('batch_id', 'channel')),
                              ('deliveries', ('day', 'channel'))]:
        # These are fixed table and column names, never configuration or model text.
        parent, foreign_key, primary_key = ('editions', 'day', 'day') if table == 'deliveries' else (
            'notification_batches', 'batch_id', 'id')
        for row in store.db.execute(f'''select q.* from {table} q join {parent} p on p.{primary_key}=q.{foreign_key}
            where q.state='failed' and q.attempts>=5 and p.expires_at>?''', (now,)):
            identity = json.dumps([table, *[row[k] for k in key_fields]])
            digest = hashlib.sha256(identity.encode()).hexdigest()
            body = (f"新闻投递任务：{row['batch_id'] if 'batch_id' in row.keys() else row['day']}\n"
                    f"渠道：{row['channel']}\n已失败 {row['attempts']} 次，达到自动重试上限。\n"
                    f"错误：{row['last_error']}。")
            result.append(('delivery:' + digest, body))
    for row in store.db.execute('select * from workflow_health where failures>=3'):
        result.append((f"command:{row['command']}:{row['first_failure']}",
                       f"流程命令：{row['command']}\n已连续失败 {row['failures']} 次。\n错误：{row['last_error']}。"))
    return result


def process_failure_alerts(settings, store) -> dict:
    cfg = settings.values.get('notifications', {}).get('failure_alerts')
    result = {'sent': 0, 'failed': 0, 'uncertain': 0, 'unconfigured': 0}
    if not cfg:return result
    now = utcnow().timestamp()
    target = next((a for a in accounts(settings) if a['id'] == cfg['account_id']), None)
    destinations = []
    if target:
        destinations = [('email', address) for address in target['emails']]
        if target['telegram_chat_id']:destinations.append(('telegram', target['telegram_chat_id']))
    destinations = [(channel, recipient) for channel, recipient in destinations
                    if settings.values.get('notifications', {}).get(channel, {}).get('enabled')]
    active = dict(incidents(store, now))
    # Falling below the rolling retry threshold does not mean an issue recovered.
    # Keep already queued publication alerts until publication actually succeeds.
    for row in store.db.execute('''select incident_id,body from failure_alert_outbox
        where incident_id like 'publication:%' and state in ('pending','failed') and expires_at>?''', (now,)):
        key = row['incident_id'].removeprefix('publication:')
        if not store.published(key):active.setdefault(row['incident_id'], row['body'])
    subject = settings.values.get('project', {}).get('name', 'Newspaper') + ' · 流程失败告警'
    for incident_id, body in active.items():
        for channel, recipient in destinations:
            store.db.execute('''insert or ignore into failure_alert_outbox
                (incident_id,account_id,channel,recipient,subject,body,expires_at) values(?,?,?,?,?,?,?)''',
                (incident_id, cfg['account_id'], channel, recipient, subject, body[:4000], now + 30 * 86400))
    interrupted = store.db.execute("update failure_alert_outbox set state='uncertain',last_error='InterruptedSend' where state='sending'")
    result['uncertain'] = interrupted.rowcount
    store.db.commit()
    rows = store.db.execute('''select * from failure_alert_outbox
        where state in ('pending','failed') and attempts<5 and next_attempt<=? and expires_at>?''', (now, now)).fetchall()
    for row in rows:
        identity = (row['incident_id'], row['account_id'], row['channel'], row['recipient'])
        where = 'incident_id=? and account_id=? and channel=? and recipient=?'
        if (row['incident_id'] not in active or row['account_id'] != cfg['account_id']
            or (row['channel'], row['recipient']) not in destinations):
            store.db.execute(f"update failure_alert_outbox set state='cancelled' where {where}", identity)
            store.db.commit();continue
        if not readiness(row['channel'], recipient=row['recipient']):
            result['unconfigured'] += 1;continue
        store.db.execute(f"update failure_alert_outbox set state='sending',attempts=attempts+1 where {where}", identity)
        store.db.commit()
        def checkpoint(part):
            store.db.execute(f'update failure_alert_outbox set progress=? where {where}', (part, *identity))
            store.db.commit()
        try:
            delivery_id = 'failure-' + hashlib.sha256(json.dumps(identity).encode()).hexdigest()[:32]
            send_text(settings, row['channel'], row['subject'], row['body'], delivery_id,
                      recipient=row['recipient'], start_part=row['progress'], on_part_sent=checkpoint)
            state = 'sent';error = None
        except UncertainDelivery:
            state = 'uncertain';error = 'UncertainDelivery'
        except Exception as exc:
            state = 'failed';error = type(exc).__name__
        result[state] += 1
        store.db.execute(f'''update failure_alert_outbox set state=?,last_error=?,next_attempt=? where {where}''',
                         (state, error, now + min(3600, 60 * 2 ** (row['attempts'] + 1)), *identity))
        store.db.commit()
    return result
