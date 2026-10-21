from __future__ import annotations

import hashlib
import json
import os
import smtplib
import ssl
import time
from email.message import EmailMessage
from email.utils import formatdate

import httpx

from .config import Settings
from .storage import Store, utcnow
from .messages import news_message, text_chunks


def readiness(channel: str, *, recipient: str | None = None) -> bool:
    keys = {"email": ["SMTP_HOST", "SMTP_USERNAME", "SMTP_PASSWORD", "EMAIL_FROM", "EMAIL_TO"],
            "telegram": ["TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"]}
    required = keys[channel][:-1] if recipient else keys[channel]
    return all(os.environ.get(k) for k in required)


def enqueue(settings: Settings, store: Store, day: str) -> None:
    for channel in ["email", "telegram"]:
        if settings.values.get("notifications", {}).get(channel, {}).get("enabled", False):
            store.db.execute("INSERT OR IGNORE INTO deliveries(day,channel) VALUES(?,?)", (day, channel))
    store.db.commit()


def send(settings: Settings, channel: str, day: str, *, start_part=0, on_part_sent=None) -> None:
    doc = json.loads((settings.data / "editions" / day / "report.json").read_text())
    subject=doc['title'];message=news_message(settings,[(day,doc)],subject)
    send_text(settings,channel,subject,message['text'],day,html_text=message['html'],telegram_parts=message['telegram'],
              start_part=start_part,on_part_sent=on_part_sent)


class UncertainDelivery(RuntimeError):
    """The provider might have accepted the message; automatic retry risks duplicates."""


def send_text(settings: Settings, channel: str, subject: str, text: str, delivery_id: str,
              *, html_text: str | None = None, telegram_parts: list | None = None,
              start_part: int = 0, on_part_sent=None, recipient: str | None = None) -> None:
    if channel == "email":
        message = EmailMessage()
        message["Subject"] = subject
        message["From"] = os.environ["EMAIL_FROM"]
        message["To"] = recipient if recipient is not None else os.environ["EMAIL_TO"]
        message["Date"] = formatdate(localtime=False)
        digest = hashlib.sha256((settings.base_url + delivery_id).encode()).hexdigest()[:24]
        message["Message-ID"] = f"<{digest}@newspaper.local>"
        message.set_content(text)
        if html_text:message.add_alternative(html_text,subtype='html')
        security = os.environ.get("SMTP_SECURITY", "starttls")
        if security not in {"starttls", "ssl"}:
            raise ValueError("SMTP requires TLS")
        cls = smtplib.SMTP_SSL if security == "ssl" else smtplib.SMTP
        kwargs = {"context": ssl.create_default_context()} if security == "ssl" else {}
        accepted=False
        try:
            with cls(os.environ["SMTP_HOST"], int(os.environ.get("SMTP_PORT", "587")), timeout=30, **kwargs) as smtp:
                if security == "starttls":
                    smtp.starttls(context=ssl.create_default_context())
                smtp.login(os.environ["SMTP_USERNAME"], os.environ["SMTP_PASSWORD"])
                try:
                    refused=smtp.send_message(message)
                    if refused:raise UncertainDelivery('Partial SMTP delivery')
                    accepted=True
                except smtplib.SMTPResponseException:raise
                except (smtplib.SMTPServerDisconnected,OSError) as exc:
                    raise UncertainDelivery('SMTP acceptance unknown') from exc
        except OSError:
            # A failed QUIT cannot undo an acknowledged DATA submission.
            if not accepted:raise
    elif channel == "telegram":
        token = os.environ["TELEGRAM_BOT_TOKEN"]
        parts=telegram_parts if telegram_parts is not None else [
            {'method':'sendMessage','text':chunk} for chunk in text_chunks(text)]
        with httpx.Client(timeout=30) as client:
            for index in range(start_part,len(parts)):
                part=parts[index];method=part['method']
                payload={k:v for k,v in part.items() if k!='method'}
                payload['chat_id']=recipient if recipient is not None else os.environ['TELEGRAM_CHAT_ID']
                if method=='sendMessage':payload['link_preview_options']={'is_disabled':True}
                if index>start_part:time.sleep(1.1)
                try:
                    response=client.post(f'https://api.telegram.org/bot{token}/{method}',json=payload)
                except (httpx.ConnectError,httpx.ConnectTimeout):raise
                except httpx.TransportError as exc:raise UncertainDelivery('Telegram acceptance unknown') from exc
                # Optional remote images can be blocked by hotlink protection. Text is already separate.
                skipped=method=='sendPhoto' and response.status_code in {400,404,413,415}
                if not skipped:
                    response.raise_for_status()
                    try:accepted=response.json().get('ok')
                    except ValueError as exc:raise UncertainDelivery('Telegram response unreadable') from exc
                    if not accepted:raise RuntimeError('Telegram rejected delivery')
                if on_part_sent:on_part_sent(index+1)
    else:
        raise ValueError("Unknown notification channel")


def deliver(settings: Settings, store: Store) -> dict:
    from .accounts import account_mode
    if account_mode(settings):
        from .batches import deliver_batches
        return deliver_batches(settings,store)
    now = utcnow().timestamp()
    interrupted=store.db.execute("UPDATE deliveries SET state='uncertain',last_error='InterruptedSend' WHERE state='sending'")
    uncertain=interrupted.rowcount;store.db.commit()
    rows = store.db.execute("""SELECT q.* FROM deliveries q JOIN editions e ON q.day=e.day
        WHERE q.state IN ('pending','failed') AND q.attempts<5 AND q.next_attempt<=? AND e.status='published' AND e.expires_at>?""", (now, now)).fetchall()
    result = {"sent": 0, "failed": 0, "unconfigured": 0}
    if uncertain:result['uncertain']=uncertain
    for row in rows:
        channel = row["channel"]
        if not settings.values.get("notifications", {}).get(channel, {}).get("enabled", False):
            continue
        if not readiness(channel):
            result["unconfigured"] += 1
            continue
        store.db.execute("UPDATE deliveries SET state='sending' WHERE day=? AND channel=?",(row['day'],channel))
        store.db.commit()
        def checkpoint(part):
            store.db.execute('UPDATE deliveries SET progress=? WHERE day=? AND channel=?',(part,row['day'],channel))
            store.db.commit()
        try:
            send(settings, channel, row["day"],start_part=row['progress'],on_part_sent=checkpoint)
            store.db.execute("UPDATE deliveries SET state='sent',attempts=attempts+1,last_error=NULL WHERE day=? AND channel=?", (row["day"], channel))
            result["sent"] += 1
        except UncertainDelivery:
            store.db.execute("UPDATE deliveries SET state='uncertain',attempts=attempts+1,last_error='UncertainDelivery' WHERE day=? AND channel=?",(row['day'],channel))
            result['uncertain']=result.get('uncertain',0)+1
        except Exception as exc:
            # Provider exception text may contain Bot tokens or URLs: persist type only.
            attempts = row["attempts"] + 1
            store.db.execute("UPDATE deliveries SET state='failed',attempts=?,next_attempt=?,last_error=? WHERE day=? AND channel=?",
                             (attempts, now + min(3600, 60 * 2 ** attempts), type(exc).__name__, row["day"], channel))
            result["failed"] += 1
        store.db.commit()
    from .batches import deliver_batches
    batched=deliver_batches(settings,store)
    for key,value in batched.items():
        if value or key in result:result[key]=result.get(key,0)+value
    return result
