from __future__ import annotations

import shutil
from datetime import date

from .config import Settings
from .publish import build
from .storage import Store, expiry, utcnow


def clean(settings: Settings, store: Store, now: float | None = None) -> dict:
    now = utcnow().timestamp() if now is None else now
    expired = [row[0] for row in store.db.execute("SELECT day FROM editions WHERE expires_at<=?", (now,))]
    # First remove expired entries from the served release. If rendering fails,
    # fail closed by deleting its dated paths and discovery indices as well.
    build_error = None
    try:
        published = build(settings, store, now)
    except Exception as exc:
        build_error = exc
        published = {"editions": len(store.editions(now)), "release": None}
        current = settings.web / "current"
        for path in [current / "index.html", current / "feed.xml", current / "search.json", *current.glob("feed-*.xml")]:
            path.unlink(missing_ok=True)
        # Archive pages also contain dated links and titles; expire them on a failed rebuild.
        shutil.rmtree(current / "archive", ignore_errors=True)
        for directory in (settings.web / "releases").iterdir():
            if directory != current.resolve():
                shutil.rmtree(directory)
                continue
            dated = set(expired)
            for path in (directory / "daily").glob("*"):
                try:
                    if expiry(path.name) <= now:
                        dated.add(path.name)
                except ValueError:
                    pass
            for day in dated:
                shutil.rmtree(directory / "daily" / day, ignore_errors=True)
                for suffix in ["md", "json"]:
                    (directory / "downloads" / f"{day}.{suffix}").unlink(missing_ok=True)
    # Include orphan directories from crashes or interrupted generation.
    removed = []
    for directory in (settings.data / "editions").iterdir():
        try:
            date.fromisoformat(directory.name)
            due = expiry(directory.name) <= now
        except ValueError:
            due = True
        if due:
            shutil.rmtree(directory)
            removed.append(directory.name)
    store.db.execute("DELETE FROM editions WHERE expires_at<=?", (now,))
    store.db.execute("DELETE FROM runs WHERE started_at<=?", (now - 30 * 86400,))
    store.db.commit()
    store.db.execute("VACUUM")
    # Caller holds the shared job lock. Nothing here can belong to an active run.
    for path in (settings.data / "tmp").iterdir():
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
        else:
            path.unlink()
    if build_error is not None:
        raise RuntimeError("Expired data deleted, but website rebuild failed") from build_error
    return {"removed": removed, **published}
