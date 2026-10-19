from __future__ import annotations

import json
import re
import shutil
import tempfile
from datetime import datetime, time, timedelta
from pathlib import Path

from jsonschema import ValidationError

from .config import TZ, Settings
from .model import Model
from .notify import deliver, enqueue
from .publish import build, markdown
from .research import gather
from .retention import clean
from .storage import Store, atomic_json, utcnow
from .validation import SCHEMA, validate_report


def generate(settings: Settings, store: Store) -> dict:
    clean(settings, store)
    now = utcnow()
    day = now.date().isoformat()
    if not settings.sections:
        return {"state": "awaiting_sections", "date": day}
    if store.published(day):
        return {"state": "already_published", "date": day, "notifications": deliver(settings, store)}
    end = datetime.combine(now.date(), time(12), TZ)
    if now < end:
        return {"state": "before_scheduled_window", "date": day}
    window = {"date": day, "window_start": (end - timedelta(days=1)).isoformat(), "window_end": end.isoformat()}
    cursor = store.db.execute("INSERT INTO runs(day,started_at,status) VALUES(?,?,'running')", (day, now.timestamp()))
    run_id = cursor.lastrowid
    store.db.commit()
    try:
        with tempfile.TemporaryDirectory(dir=settings.data / "tmp", prefix="run-") as temp:
            work = Path(temp)
            collections = []
            for index, section in enumerate(settings.sections):
                collections.append(gather(settings, section, window, work / f"collect-{index}"))
            if not any(group["sources"] for group in collections):
                raise ValueError("No fetched source material; refusing to publish an empty success")
            history = store.history()
            client = Model(settings)
            feedback: dict = {}
            for attempt in range(2):
                sources = list({s["id"]: s for c in collections for s in c["sources"]}.values())
                payload = {"window": window, "sections": settings.sections, "collections": collections,
                           "history": history, "schema": SCHEMA, "feedback": feedback}
                doc = client.request("edit", payload)
                doc.update({"schema_version": 1, **window, "generated_at": utcnow().isoformat(),
                            "sources": [{k: s[k] for k in ["id", "url", "title", "published_at"]} for s in sources]})
                try:
                    validate_report(doc, settings, sources, history, window)
                except (ValueError, ValidationError) as exc:
                    feedback = {"validation_error": str(exc)[:200]}
                    if attempt == 0:
                        continue
                    raise
                review = client.request("review", {**payload, "report": doc})
                if review.get("ok") is True and not review.get("problems") and not review.get("research_queries"):
                    break
                if attempt == 1:
                    raise ValueError("Independent evidence review rejected the report")
                feedback = review
                by_id = {s["id"]: s for s in settings.sections}
                for index, query in enumerate(review.get("research_queries", [])[:3]):
                    if query.get("section_id") in by_id and isinstance(query.get("query"), str):
                        collections.append(gather(settings, by_id[query["section_id"]], window,
                                                  work / f"supplement-{index}", query["query"]))
            edition = settings.data / "editions" / day
            edition.mkdir(exist_ok=True)
            atomic_json(edition / "report.json", doc)
            atomic_json(edition / "raw.json", {"window": window, "collections": collections})
            (edition / "report.md").write_text(markdown(doc))
            # A previous failed build can leave a ready row; published rows were
            # handled above and are never silently replaced.
            store.db.execute("DELETE FROM editions WHERE day=? AND status!='published'", (day,))
            store.db.commit()
            store.add(doc)
            build(settings, store)
            store.db.execute("UPDATE editions SET status='published' WHERE day=?", (day,))
            enqueue(settings, store, day)
            store.db.execute("UPDATE runs SET status='published',finished_at=? WHERE id=?", (utcnow().timestamp(), run_id))
            store.db.commit()
            return {"state": "published", "date": day, "notifications": deliver(settings, store)}
    except BaseException as exc:
        store.db.execute("UPDATE runs SET status='failed',finished_at=?,error=? WHERE id=?",
                         (utcnow().timestamp(), type(exc).__name__, run_id))
        store.db.commit()
        # Never persist raw exception messages: HTTP exceptions may expose keys.
        raise


def import_legacy(settings: Settings, store: Store, source: Path) -> dict:
    if not source.is_dir():
        raise ValueError("Legacy directory is missing or not accessible to this user")
    count = 0
    for path in sorted(source.glob("????-??-?? 新闻总结.md")):
        day = path.name[:10]
        from .storage import expiry
        if expiry(day) <= utcnow().timestamp() or store.published(day):
            continue
        doc = {"schema_version": 1, "date": day, "title": day + " 新闻总结", "summary": "旧版日报存档，原文保留。",
               "generated_at": day + "T12:00:00+08:00", "sections": [], "sources": [], "legacy": True}
        directory = settings.data / "editions" / day
        directory.mkdir(exist_ok=True)
        shutil.copyfile(path, directory / "report.md")
        atomic_json(directory / "report.json", doc)
        store.add(doc, legacy=True)
        ledger = re.search(r"<!--\s*news-ledger\s*\n(.*?)-->", path.read_text(), re.S)
        if ledger:
            for line in ledger.group(1).splitlines():
                parts = line.split("|", 2)
                if len(parts) == 3 and parts[1].strip():
                    description = parts[1].strip()
                    store.db.execute("INSERT OR IGNORE INTO events VALUES(?,?,?)", (description, day, description))
        store.db.execute("UPDATE editions SET status='published' WHERE day=?", (day,))
        store.db.commit()
        count += 1
    result = clean(settings, store)
    return {"imported": count, **result}
