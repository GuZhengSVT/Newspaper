from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import re
import unicodedata
import sqlite3
import tempfile
import time as clock
from datetime import date, datetime, time, timedelta
from pathlib import Path

from .config import TZ, Settings


def utcnow() -> datetime:
    return datetime.now(TZ)


def edition_key(doc: dict) -> str:
    day = date.fromisoformat(doc["date"]).isoformat()
    slug = doc.get("edition", "")
    if slug and not re.fullmatch(r"[a-z][a-z0-9-]{0,40}", slug):
        raise ValueError("Unsafe edition id")
    return day + ("/" + slug if slug else "")


def normalize_entity(text: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKC", text).casefold() if c.isalnum())


def expiry(day: str, timezone=TZ) -> float:
    # Rebuilds never extend retention. Keep the edition date and the next 29 dates.
    return datetime.combine(date.fromisoformat(day.split("/")[0]) + timedelta(days=30), time(), timezone).timestamp()


def atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=".write-")
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


@contextlib.contextmanager
def lock(settings: Settings, wait_seconds: float = 0):
    settings.prepare()
    with (settings.data / "pipeline.lock").open("a") as stream:
        deadline = clock.monotonic() + wait_seconds
        while True:
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if clock.monotonic() >= deadline:
                    raise BlockingIOError("Another Newspaper job is already running")
                clock.sleep(0.05)
        yield


class Store:
    def __init__(self, settings: Settings, *, read_only: bool = False):
        self.settings = settings
        database = settings.data / "newspaper.sqlite"
        if read_only:
            self.db = sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)
            self.db.row_factory = sqlite3.Row
            return
        settings.prepare()
        self.db = sqlite3.connect(database)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
          PRAGMA foreign_keys=ON;
          PRAGMA secure_delete=ON;
          PRAGMA journal_mode=DELETE;
          CREATE TABLE IF NOT EXISTS editions (
            day TEXT PRIMARY KEY, expires_at REAL NOT NULL, status TEXT NOT NULL,
            title TEXT NOT NULL, summary TEXT NOT NULL, legacy INTEGER NOT NULL DEFAULT 0,
            notify_suppressed INTEGER NOT NULL DEFAULT 0
          );
          CREATE TABLE IF NOT EXISTS events (
            id TEXT NOT NULL, day TEXT NOT NULL REFERENCES editions(day) ON DELETE CASCADE,
            description TEXT NOT NULL, PRIMARY KEY(id, day)
          );
          CREATE TABLE IF NOT EXISTS deliveries (
            day TEXT NOT NULL REFERENCES editions(day) ON DELETE CASCADE, channel TEXT NOT NULL,
            state TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
            next_attempt REAL NOT NULL DEFAULT 0, last_error TEXT,
            PRIMARY KEY(day, channel)
          );
          CREATE TABLE IF NOT EXISTS runs (
            id INTEGER PRIMARY KEY, day TEXT NOT NULL, started_at REAL NOT NULL,
            finished_at REAL, status TEXT NOT NULL, error TEXT
          );
          CREATE TABLE IF NOT EXISTS event_entities (
            entity TEXT NOT NULL, event_id TEXT NOT NULL,
            day TEXT NOT NULL REFERENCES editions(day) ON DELETE CASCADE,
            PRIMARY KEY(entity,event_id,day)
          );
          CREATE TABLE IF NOT EXISTS event_sources (
            url TEXT NOT NULL, event_id TEXT NOT NULL, day TEXT NOT NULL,
            PRIMARY KEY(url,event_id,day),
            FOREIGN KEY (event_id,day) REFERENCES events(id,day) ON DELETE CASCADE
          );
          CREATE INDEX IF NOT EXISTS entity_lookup ON event_entities(entity);
          CREATE TABLE IF NOT EXISTS publication_tasks (
            day TEXT PRIMARY KEY REFERENCES editions(day) ON DELETE CASCADE, task_id TEXT NOT NULL
          );
          CREATE INDEX IF NOT EXISTS publication_task_lookup ON publication_tasks(task_id);
          CREATE TABLE IF NOT EXISTS notification_batches (
            id TEXT PRIMARY KEY, task_id TEXT NOT NULL, period TEXT NOT NULL,
            window_start TEXT NOT NULL, window_end TEXT NOT NULL,
            expected TEXT NOT NULL, channels TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'waiting',
            expires_at REAL NOT NULL
          );
          CREATE TABLE IF NOT EXISTS notification_outbox (
            batch_id TEXT NOT NULL REFERENCES notification_batches(id) ON DELETE CASCADE,
            channel TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
            next_attempt REAL NOT NULL DEFAULT 0, last_error TEXT, PRIMARY KEY(batch_id,channel)
          );
          CREATE TABLE IF NOT EXISTS account_outbox (
            batch_id TEXT NOT NULL REFERENCES notification_batches(id) ON DELETE CASCADE,
            account_id TEXT NOT NULL, channel TEXT NOT NULL, recipient TEXT NOT NULL, options TEXT NOT NULL,
            state TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
            progress INTEGER NOT NULL DEFAULT 0, next_attempt REAL NOT NULL DEFAULT 0, last_error TEXT,
            PRIMARY KEY(batch_id,account_id,channel,recipient)
          );
          CREATE TABLE IF NOT EXISTS workflow_health (
            command TEXT PRIMARY KEY, failures INTEGER NOT NULL, first_failure REAL NOT NULL,
            updated_at REAL NOT NULL, last_error TEXT NOT NULL
          );
          CREATE TABLE IF NOT EXISTS failure_alert_outbox (
            incident_id TEXT NOT NULL, account_id TEXT NOT NULL, channel TEXT NOT NULL,
            recipient TEXT NOT NULL, subject TEXT NOT NULL, body TEXT NOT NULL,
            state TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
            progress INTEGER NOT NULL DEFAULT 0, next_attempt REAL NOT NULL DEFAULT 0,
            last_error TEXT, expires_at REAL NOT NULL,
            PRIMARY KEY(incident_id,account_id,channel,recipient)
          );
        """)
        if 'notify_suppressed' not in {r[1] for r in self.db.execute('PRAGMA table_info(editions)')}:
            self.db.execute('ALTER TABLE editions ADD COLUMN notify_suppressed INTEGER NOT NULL DEFAULT 0')
        columns = {r[1] for r in self.db.execute("PRAGMA table_info(events)")}
        if "facts" not in columns:
            self.db.execute("ALTER TABLE events ADD COLUMN facts TEXT NOT NULL DEFAULT '[]'")
        if "entities" not in columns:
            self.db.execute("ALTER TABLE events ADD COLUMN entities TEXT NOT NULL DEFAULT '[]'")
        if "evidence_metadata" not in columns:
            self.db.execute("ALTER TABLE events ADD COLUMN evidence_metadata TEXT NOT NULL DEFAULT '{}'")
        if "metrics" not in {r[1] for r in self.db.execute("PRAGMA table_info(runs)")}:
            self.db.execute("ALTER TABLE runs ADD COLUMN metrics TEXT NOT NULL DEFAULT '{}'")
        if 'progress' not in {r[1] for r in self.db.execute('PRAGMA table_info(notification_outbox)')}:
            self.db.execute('ALTER TABLE notification_outbox ADD COLUMN progress INTEGER NOT NULL DEFAULT 0')
        if 'progress' not in {r[1] for r in self.db.execute('PRAGMA table_info(deliveries)')}:
            self.db.execute('ALTER TABLE deliveries ADD COLUMN progress INTEGER NOT NULL DEFAULT 0')
        if 'correction_note' not in {r[1] for r in self.db.execute('PRAGMA table_info(notification_batches)')}:
            self.db.execute("ALTER TABLE notification_batches ADD COLUMN correction_note TEXT NOT NULL DEFAULT ''")
        self.db.commit()

    def close(self) -> None:
        self.db.close()

    def editions(self, now: float | None = None) -> list[dict]:
        return [dict(r) for r in self.db.execute(
            "SELECT * FROM editions WHERE expires_at>? AND status IN ('ready','published') ORDER BY day DESC",
            (now if now is not None else utcnow().timestamp(),),
        )]

    def add(self, doc: dict, legacy: bool = False, task_id: str | None = None, *, suppress_notification=False) -> None:
        key = edition_key(doc)
        self.db.execute(
            "INSERT INTO editions(day,expires_at,status,title,summary,legacy,notify_suppressed) VALUES(?,?,'ready',?,?,?,?)",
            (key, expiry(doc["date"],self.settings.timezone), doc["title"], doc["summary"], int(legacy), int(suppress_notification)),
        )
        if task_id:self.db.execute("INSERT INTO publication_tasks(day,task_id) VALUES(?,?)",(key,task_id))
        from .events import core_claims, event_evidence
        urls = {source['id']: source['url'] for source in doc.get('sources', [])}
        for section in doc.get("sections", []):
            for article in section["articles"]:
                entities = article.get("entities", [])
                evidence = event_evidence(article, urls)
                metadata = {'window_end': doc.get('window_end'), 'claims': sorted(core_claims(article)),
                            'evidence': sorted(evidence)}
                self.db.execute("INSERT INTO events(id,day,description,facts,entities,evidence_metadata) VALUES(?,?,?,?,?,?)",
                                (article["event_key"], key, article["title"],
                                 json.dumps(([article["new_facts"]] if article["new_facts"] else []) + [f["text"][:500] for f in article["facts"] if f.get("kind", "fact") == "fact"][:4], ensure_ascii=False),
                                 json.dumps(entities, ensure_ascii=False), json.dumps(metadata, ensure_ascii=False)))
                for url in {url for url, _ in evidence}:
                    self.db.execute("INSERT INTO event_sources(url,event_id,day) VALUES(?,?,?)",
                                    (url, article["event_key"], key))
                for entity in entities:
                    normalized = normalize_entity(entity)
                    if normalized:
                        self.db.execute("INSERT OR IGNORE INTO event_entities VALUES(?,?,?)",
                                        (normalized, article["event_key"], key))
        self.db.commit()

    def set_task(self, day: str, task_id: str | None) -> None:
        if task_id:
            self.db.execute('''insert into publication_tasks(day,task_id) values(?,?)
                on conflict(day) do update set task_id=excluded.task_id''',(day,task_id))
            self.db.commit()

    def existing_publication(self, job: dict, window: dict) -> str | None:
        """A group rename does not create a new issue for the same sections and slot."""
        key=window['date']+'/'+job['id']
        if self.db.execute("select 1 from editions where day=? and status in ('ready','published')",(key,)).fetchone():
            return key
        rows=self.db.execute("""select e.day from editions e join publication_tasks t on t.day=e.day
            where (t.task_id=? or e.day like ?) and e.status in ('ready','published') and e.expires_at>?""",
            (job['task_id'],window['date']+'/%',utcnow().timestamp())).fetchall()
        matches=[]
        for row in rows:
            doc=self.document(row['day'])
            if (all(doc.get(k)==window[k] for k in ['date','window_start','window_end'])
                and {s['id'] for s in doc.get('sections',[])}==set(job['sections'])):
                matches.append((doc['generated_at'],row['day']))
        return max(matches)[1] if matches else None

    def history(self, task_id: str | None = None) -> list[dict]:
        return [dict(r) for r in self.db.execute(
            """SELECT e.id,e.description,e.day FROM events e JOIN editions d ON e.day=d.day
               LEFT JOIN publication_tasks t ON t.day=d.day
               WHERE d.expires_at>? AND d.status='published' AND (? IS NULL OR t.task_id=?)""",
            (utcnow().timestamp(),task_id,task_id),
        )]

    def related_history(self, candidates: list[dict], limit: int = 40, task_id: str | None = None,
                        *, sources: list[dict] | None = None, window: dict | None = None) -> list[dict]:
        from .events import core_claims, event_evidence
        from .research import source_url
        entities = {normalize_entity(e) for c in candidates for e in c.get("entities", [])}
        entities.discard("")
        keys = {c["event_key"].strip().casefold() for c in candidates if c.get("event_key")}
        source_ids = {sid for candidate in candidates for sid in candidate.get('source_ids', [])}
        urls = {source_url(s['url']) for s in sources or [] if s['id'] in source_ids}
        if not entities and not keys and not urls:
            return []
        scope = task_id if sources is None else None
        marks = ",".join("?" for _ in entities) or "NULL"
        keymarks = ",".join("?" for _ in keys) or "NULL"
        urlmarks = ",".join("?" for _ in urls) or "NULL"
        now = utcnow().timestamp()
        latest_day = (date.fromisoformat(window['date']) + timedelta(days=1)).isoformat() if window else None
        cutoff = datetime.fromisoformat(window['window_end']) if window else None
        rows = self.db.execute(f"""SELECT e.*,t.task_id FROM events e
            JOIN editions d ON d.day=e.day
            LEFT JOIN publication_tasks t ON t.day=d.day
            WHERE d.status='published' AND d.expires_at>? AND (? IS NULL OR t.task_id=?)
                AND (? IS NULL OR e.day<?)
                AND (lower(trim(e.id)) IN ({keymarks})
                    OR EXISTS(SELECT 1 FROM event_entities x WHERE x.day=e.day AND x.event_id=e.id AND x.entity IN ({marks}))
                    OR EXISTS(SELECT 1 FROM event_sources s WHERE s.day=e.day AND s.event_id=e.id AND s.url IN ({urlmarks})))
            ORDER BY (lower(trim(e.id)) IN ({keymarks})) DESC,(t.task_id IS ?) DESC,e.day DESC,e.id LIMIT ?""",
            (now, scope, scope, latest_day, latest_day, *keys, *entities, *urls, *keys, task_id, limit)).fetchall()
        known = {(r["id"], r["day"]) for r in rows}
        if entities and len(rows) < limit:
            legacy = self.db.execute("""SELECT e.*,t.task_id FROM events e JOIN editions d ON d.day=e.day
                LEFT JOIN publication_tasks t ON t.day=d.day WHERE d.status='published' AND d.expires_at>?
                AND e.entities='[]' AND (? IS NULL OR t.task_id=?)
                AND (? IS NULL OR e.day<?) ORDER BY e.day DESC""", (now,scope,scope,latest_day,latest_day))
            for row in legacy:
                if (row["id"], row["day"]) not in known and any(len(e)>=3 and e in normalize_entity(row["description"]) for e in entities):
                    rows.append(row)
                    if len(rows) >= limit:
                        break
        documents = {}
        result = []
        for row in rows:
            entry = dict(row)
            metadata = json.loads(entry.pop('evidence_metadata'))
            if not metadata and sources is not None:
                day = row['day']
                if day not in documents:
                    try:
                        documents[day] = self.document(day)
                    except (OSError, ValueError):
                        documents[day] = {}
                doc = documents[day]
                old = next((a for sec in doc.get('sections', []) for a in sec['articles'] if a['event_key']==row['id']), None)
                if old:
                    old_urls = {s['id']: s['url'] for s in doc.get('sources', [])}
                    metadata = {'claims': sorted(core_claims(old)), 'evidence': sorted(event_evidence(old, old_urls)),
                                'window_end': doc.get('window_end')}
            if cutoff is not None and metadata.get('window_end') and datetime.fromisoformat(metadata['window_end']) > cutoff:
                continue
            result.append({**entry, **metadata, 'facts': json.loads(row['facts']), 'entities': json.loads(row['entities']),
                           'same_task': row['task_id'] == task_id})
        return result

    def reuse_event_evidence(self, candidates: list[dict], sources: list[dict], history: list[dict],
                             *, window: dict | None = None) -> list[dict]:
        from .research import source_url
        from .validation import normalize
        current_urls = {source_url(s['url']) for s in sources}
        if window is not None:
            end = datetime.fromisoformat(window['window_end'])
            history = [row for row in history if not row.get('window_end')
                       or datetime.fromisoformat(row['window_end']) <= end]
        archives = {}
        reused = {}
        now = utcnow().timestamp()
        for candidate in candidates:
            prior = next((row for row in history if normalize(row['id']) == normalize(candidate['event_key'])), None)
            if not prior or not prior.get('evidence'):
                continue
            day = prior['day']
            if day not in archives:
                retained = self.db.execute("SELECT 1 FROM editions WHERE day=? AND status='published' AND expires_at>?",
                                           (day, now)).fetchone()
                try:
                    archives[day] = self.document(day) if retained else {}
                except (OSError, ValueError):
                    archives[day] = {}
            archive = archives[day]
            article = next((a for section in archive.get('sections', []) for a in section['articles']
                            if a['event_key'] == prior['id']), None)
            if not article:
                continue
            records = {s['id']: s for s in archive.get('sources', [])}
            quotes = {}
            for fact in article['facts']:
                for item in fact['evidence']:
                    quotes.setdefault(item['source_id'], []).append(item['quote'])
            for source_id, excerpts in list(quotes.items())[:3]:
                record = records.get(source_id)
                if record is None or source_url(record['url']) in current_urls:
                    continue
                digest = hashlib.sha256(json.dumps([day, source_id, prior["id"]]).encode()).hexdigest()
                stable_id = "archive:" + digest
                if stable_id not in reused:
                    reused[stable_id] = {**record, 'id': stable_id,
                                         'content': '\n[…]\n'.join(dict.fromkeys(excerpts)),
                                         'prior_edition': day, 'prior_event_key': prior['id']}
                if stable_id not in candidate['source_ids']:
                    candidate['source_ids'].append(stable_id)
        return [*sources, *reused.values()]

    def document(self, day: str) -> dict:
        parts = day.split("/")
        date.fromisoformat(parts[0])
        if len(parts) > 2 or (len(parts) == 2 and not re.fullmatch(r"[a-z][a-z0-9-]{0,40}", parts[1])):
            raise ValueError("Unsafe document path")
        return json.loads((self.settings.data / "editions" / day / "report.json").read_text())

    def published(self, day: str) -> bool:
        return self.db.execute("SELECT 1 FROM editions WHERE day=? AND status='published'", (day,)).fetchone() is not None
