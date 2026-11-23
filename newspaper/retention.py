from __future__ import annotations

import json
import shutil
from datetime import date, datetime, timedelta

from .config import Settings
from .publish import build, render_inputs
from .storage import Store, expiry, utcnow


def clean(settings: Settings, store: Store, now: float | None = None) -> dict:
    now = utcnow().timestamp() if now is None else now
    expired = [row[0] for row in store.db.execute("SELECT day FROM editions WHERE expires_at<=?", (now,))]
    current = settings.web / "current"
    local_day = datetime.fromtimestamp(now, settings.timezone).date()
    today = local_day.isoformat()
    entries = store.editions(now)
    state_file = settings.data / 'site-inputs.json'
    expired_days = {key.split('/')[0] for key in expired}
    for dirname in ('daily', 'downloads'):
        for path in (current / dirname).glob('[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]*'):
            day = path.name[:10]
            try:
                expired_path = expiry(day, settings.timezone) <= now
            except ValueError:
                continue
            if expired_path:
                expired_days.add(day)
    build_error = None
    published = {'editions': len(entries), 'release': current.resolve().name, 'changed': False}
    try:
        try:
            saved = json.loads(state_file.read_text())
        except (FileNotFoundError, ValueError):
            saved = {}
        if (expired_days or not isinstance(saved, dict) or not (current / 'site-version.txt').is_file()
                or not (current / 'site-manifest.json').is_file()
                or not (current / 'index.html').is_file()
                or saved.get('release') != current.resolve().name
                or saved.get('inputs') != render_inputs(settings, entries, today)):
            published = build(settings, store, now)
    except Exception as exc:
        build_error = exc
        if expired_days:
            for name in ('index.html', 'feed.xml', 'search.json', 'site-version.txt', 'site-manifest.json'):
                (current / name).unlink(missing_ok=True)
            for path in current.glob('feed-*.xml'):
                path.unlink()
            shutil.rmtree(current / "archive", ignore_errors=True)
            for day in expired_days:
                shutil.rmtree(current / "daily" / day, ignore_errors=True)
                shutil.rmtree(current / "downloads" / day, ignore_errors=True)
                for suffix in ["md", "json"]:
                    (current / "downloads" / f"{day}.{suffix}").unlink(missing_ok=True)
    for directory in (settings.web / 'releases').iterdir():
        if directory != current.resolve():
            shutil.rmtree(directory)
    # Include orphan directories from crashes or interrupted generation.
    removed = []
    for directory in (settings.data / "editions").iterdir():
        try:
            date.fromisoformat(directory.name)
            due = expiry(directory.name,settings.timezone) <= now
        except ValueError:
            due = True
        if due:
            shutil.rmtree(directory)
            removed.append(directory.name)
    changes_before = store.db.total_changes
    store.db.execute("DELETE FROM notification_batches WHERE expires_at<=?", (now,))
    store.db.execute("DELETE FROM editions WHERE expires_at<=?", (now,))
    store.db.execute("DELETE FROM runs WHERE started_at<=?", (now - 30 * 86400,))
    store.db.execute("DELETE FROM failure_alert_outbox WHERE expires_at<=?", (now,))
    store.db.execute("DELETE FROM workflow_health WHERE updated_at<=?", (now - 30 * 86400,))
    if store.db.execute("SELECT 1 FROM sqlite_master WHERE name='model_token_usage'").fetchone():
        store.db.execute("DELETE FROM model_token_usage WHERE substr(day,1,10)<=?",
                         ((local_day - timedelta(days=30)).isoformat(),))
    store.db.commit()
    if store.db.total_changes > changes_before:
        free = store.db.execute('PRAGMA freelist_count').fetchone()[0]
        pages = store.db.execute('PRAGMA page_count').fetchone()[0]
        if free >= 64 and free * 4 >= pages:
            store.db.execute("VACUUM")
    # Caller holds the shared job lock. Nothing here can belong to an active run.
    for path in (settings.data / "tmp").iterdir():
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
        else:
            path.unlink()
    if build_error is not None:
        raise RuntimeError("Website maintenance could not finish the build") from build_error
    return {"removed": removed, **published}
