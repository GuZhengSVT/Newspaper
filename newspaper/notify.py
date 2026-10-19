from __future__ import annotations

import hashlib
import json
import os
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formatdate

import httpx

from .config import Settings
from .storage import Store, utcnow


def readiness(channel: str) -> bool:
    keys = {"email": ["SMTP_HOST", "SMTP_USERNAME", "SMTP_PASSWORD", "EMAIL_FROM", "EMAIL_TO"],
            "telegram": ["TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"]}
    return all(os.environ.get(k) for k in keys[channel])


def enqueue(settings: Settings, store: Store, day: str) -> None:
    for channel in ["email", "telegram"]:
        if settings.values.get("notifications", {}).get(channel, {}).get("enabled", False):
            store.db.execute("INSERT OR IGNORE INTO deliveries(day,channel) VALUES(?,?)", (day, channel))
    store.db.commit()


def send(settings: Settings, channel: str, day: str) -> None:
    # Push links, not full articles or attachments: retention applies on this VPS.
    label = json.loads((settings.data / "editions" / day / "report.json").read_text())["title"]
    text = f"{label} Newspaper 新闻已发布。\n阅读：{settings.base_url}/daily/{day}/\nMarkdown：{settings.base_url}/downloads/{day}.md\n服务器仅保留最近 30 天。"
    send_text(settings,channel,f"Newspaper · {label} 已更新",text,day)


class UncertainDelivery(RuntimeError):
    """The provider might have accepted the message; automatic retry risks duplicates."""


def send_text(settings: Settings, channel: str, subject: str, text: str, delivery_id: str) -> None:
    if channel == "email":
        message = EmailMessage()
        message["Subject"] = subject
        message["From"] = os.environ["EMAIL_FROM"]
        message["To"] = os.environ["EMAIL_TO"]
        message["Date"] = formatdate(localtime=False)
        digest = hashlib.sha256((settings.base_url + delivery_id).encode()).hexdigest()[:24]
        message["Message-ID"] = f"<{digest}@newspaper.local>"
        message.set_content(text)
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
        with httpx.Client(timeout=30) as client:
            try:
                response = client.post(f"https://api.telegram.org/bot{token}/sendMessage", json={
                    "chat_id": os.environ["TELEGRAM_CHAT_ID"], "text": text, "link_preview_options": {"is_disabled": True},
                })
            except (httpx.ConnectError,httpx.ConnectTimeout):raise
            except httpx.TransportError as exc:raise UncertainDelivery('Telegram acceptance unknown') from exc
            response.raise_for_status()
            try:accepted=response.json().get('ok')
            except ValueError as exc:raise UncertainDelivery('Telegram response unreadable') from exc
            if not accepted:
                raise RuntimeError("Telegram rejected delivery")
    else:
        raise ValueError("Unknown notification channel")


def deliver(settings: Settings, store: Store) -> dict:
    now = utcnow().timestamp()
    rows = store.db.execute("""SELECT q.* FROM deliveries q JOIN editions e ON q.day=e.day
        WHERE q.state!='sent' AND q.attempts<5 AND q.next_attempt<=? AND e.status='published' AND e.expires_at>?""", (now, now)).fetchall()
    result = {"sent": 0, "failed": 0, "unconfigured": 0}
    for row in rows:
        channel = row["channel"]
        if not settings.values.get("notifications", {}).get(channel, {}).get("enabled", False):
            continue
        if not readiness(channel):
            result["unconfigured"] += 1
            continue
        try:
            send(settings, channel, row["day"])
            store.db.execute("UPDATE deliveries SET state='sent',attempts=attempts+1,last_error=NULL WHERE day=? AND channel=?", (row["day"], channel))
            result["sent"] += 1
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
