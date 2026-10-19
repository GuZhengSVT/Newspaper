from __future__ import annotations

import copy
import json
import re
import shutil
import tempfile
from datetime import datetime, time, timedelta
from pathlib import Path

from jsonschema import ValidationError

from .config import TZ, Settings
from .bounded import research as bounded_research, supplement as bounded_supplement
from .model import Model
from .evidence import selected_sources, used_evidence, validate_selection
from .notify import deliver, enqueue
from .publish import build, markdown
from .research import gather
from .retention import clean
from .storage import Store, atomic_json, utcnow
from .validation import SCHEMA, validate_report, normalize


def generate(settings: Settings, store: Store, notify: bool = True) -> dict:
    """Both editions share a bounded in-run source cache; publications are independent."""
    clean(settings, store)
    if not settings.sections:
        return {"state": "awaiting_sections", "date": utcnow().date().isoformat()}
    if utcnow().hour < 12:
        return {"state": "before_scheduled_window", "date": utcnow().date().isoformat()}
    cache = {}
    results = []
    for edition in settings.editions:
        scoped = settings.for_edition(edition)
        try:
            results.append(generate_edition(scoped, store, edition, cache, notify))
        except Exception as exc:
            results.append({"edition": edition["id"], "state": "failed", "error_type": type(exc).__name__})
            if not edition["id"]:  # Preserve the original single-edition CLI contract.
                raise
    if len(results) == 1 and not settings.editions[0]["id"]:
        return results[0]
    state = "published" if all(r["state"] in {"published", "already_published"} for r in results) else "partial_failure"
    return {"state": state, "date": utcnow().date().isoformat(), "editions": results}


def generate_edition(settings: Settings, store: Store, edition: dict, cache: dict, notify: bool) -> dict:
    now = utcnow()
    day = now.date().isoformat()
    key = day + ("/" + edition["id"] if edition["id"] else "")
    if store.published(key):
        return {"state": "already_published", "edition": edition["id"], "date": day}
    end = datetime.combine(now.date(), time(12), TZ)
    window = {"date": day, "window_start": (end - timedelta(days=1)).isoformat(), "window_end": end.isoformat()}
    cursor = store.db.execute("INSERT INTO runs(day,started_at,status) VALUES(?,?,'running')", (key, now.timestamp()))
    run_id = cursor.lastrowid
    store.db.commit()
    client = Model(settings)
    metrics = {"dsh_calls": 0, "dsh_tokens": None, "cache_sources": 0}
    try:
        with tempfile.TemporaryDirectory(dir=settings.data / "tmp", prefix="run-") as temp:
            work = Path(temp)
            collections = []
            coverage = []
            if settings.values.get("search", {}).get("mode") == "bounded":
                sources, candidates, coverage, _, search_metrics = bounded_research(settings, client, window, work, cache)
                metrics.update(search_metrics, dsh_tokens=0)
                history = store.related_history(candidates)
                contexts = selected_sources(sources, candidates)
            else:
                # Batches are explicitly configured related topics, never an arbitrary single mega-query.
                batches = {}
                for section in settings.sections:
                    batches.setdefault(section.get("batch", section["id"]), []).append(section)
                for index, (name, sections) in enumerate(batches.items()):
                    group = sections[0] if len(sections) == 1 else {
                        "id": name, "name": "、".join(s["name"] for s in sections),
                        "scope": "\n".join(s["name"] + "：" + s.get("scope", "") for s in sections),
                        "sections": sections, "query_count": max(3, len(sections) * 2)}
                    metrics["dsh_calls"] += 1
                    try:
                        collections.append(gather(settings, group, window, work / f"collect-{index}", cache=cache))
                    except Exception as exc:
                        coverage.append(group["name"] + "检索未完成（" + type(exc).__name__ + "），本期覆盖不完整。")
                sources = list({s["id"]: s for c in collections for s in c["sources"]}.values())
                if not sources:
                    raise ValueError("No fetched source material; refusing to publish an empty success")
                if edition["id"]:
                    limit = settings.values.get("search", {}).get("max_candidates_per_edition", 12)
                    selection = client.request("select", {"window": window, "sections": settings.sections,
                        "sources": [{**s, "content": s["content"][:2200]} for s in sources],
                        "candidate_limit": limit})
                    candidates = validate_selection(selection, settings, sources, limit)
                    coverage.extend(str(n)[:600] for n in selection.get("coverage_notes", [])[:15])
                    history = store.related_history(candidates)
                    contexts = selected_sources(sources, candidates)
                else:
                    candidates = []
                    history = store.history()
                    contexts = sources
            feedback = {}
            repair_keys = None
            for attempt in range(2):
                payload = {"window": window, "edition": edition, "sections": settings.sections,
                           "sources": contexts, "candidates": candidates,
                           "history": history, "schema": SCHEMA, "feedback": feedback}
                if attempt and repair_keys and edition["id"]:
                    scoped_candidates = [c for c in candidates if c["event_key"] in repair_keys]
                    repair_contexts = selected_sources(sources, scoped_candidates) if edition["id"] else contexts
                    patch = client.request("repair", {"window": window, "report": doc, "event_keys": repair_keys,
                        "sources": repair_contexts, "history": history, "feedback": feedback})
                    doc = apply_repair(doc, patch, set(repair_keys))
                else:
                    doc = client.request("edit", payload)
                doc.update({"schema_version": 2 if edition["id"] else 1, **window,
                            "generated_at": utcnow().isoformat()})
                if edition["id"]:
                    doc["edition"] = edition["id"]
                    doc["title"] = day + " " + edition["name"]
                    doc["coverage_notes"] = list(dict.fromkeys(coverage + doc.get("coverage_notes", [])))[:30]
                # Only references actually cited in the draft enter the public report.
                used = {e["source_id"] for section in doc["sections"] for article in section["articles"]
                        for fact in article["facts"] for e in fact["evidence"]}
                doc["sources"] = [{k: src[k] for k in ["id", "url", "title", "published_at"]}
                                  for src in contexts if src["id"] in used]
                try:
                    validate_report(doc, settings, contexts, history, window)
                    if edition["id"]:
                        planned = {c["event_key"] for c in candidates}
                        if any(a["event_key"] not in planned for sec in doc["sections"] for a in sec["articles"]):
                            raise ValueError("Unselected event in report")
                        all_keys = {normalize(e["id"]) for e in store.history()}
                        if any(normalize(a["event_key"]) in all_keys and not a["follow_up"]
                               for sec in doc["sections"] for a in sec["articles"]):
                            raise ValueError("Previously published event")
                except (ValueError, ValidationError) as exc:
                    feedback = {"validation_error": str(exc)[:300]}
                    if attempt == 0:
                        continue
                    raise
                evidence = used_evidence(doc, contexts)
                review = client.request("review", {"window": window, "sections": settings.sections,
                    "report": doc, "sources": evidence, "history": history, "candidates": candidates,
                    "collection_succeeded": True})
                if review.get("ok") is True and not review.get("problems") and not review.get("research_queries"):
                    break
                if attempt == 1:
                    raise ValueError("Evidence review rejected the report")
                feedback = review
                existing = {a["event_key"] for sec in doc["sections"] for a in sec["articles"]}
                affected = review.get("affected_event_keys", [])
                repair_keys = [k for k in affected if k in existing] or list(existing)
                by_id = {s["id"]: s for s in settings.sections}
                for index, query in enumerate(review.get("research_queries", [])[:3]):
                    if query.get("section_id") in by_id and isinstance(query.get("query"), str):
                        if settings.values.get("search", {}).get("mode") == "bounded":
                            extra_sources = bounded_supplement(settings, by_id[query["section_id"]], query["query"],
                                                              work / f"supplement-{index}", cache)
                            metrics["plugin_queries"] += 1
                        else:
                            metrics["dsh_calls"] += 1
                            extra_sources = gather(settings, by_id[query["section_id"]], window,
                                work / f"supplement-{index}", query["query"], cache=cache)["sources"]
                        sources.extend(extra_sources)
                        # Supplement sources serve existing candidates only.
                        for candidate in candidates:
                            if candidate["section_id"] == query["section_id"]:
                                candidate["source_ids"] += [s["id"] for s in extra_sources]
                        contexts = selected_sources(list({s["id"]: s for s in sources}.values()), candidates) if edition["id"] else sources
            directory = settings.data / "editions" / key
            directory.mkdir(parents=True, exist_ok=True)
            atomic_json(directory / "report.json", doc)
            atomic_json(directory / "evidence.json", {"window": window, "sources": used_evidence(doc, contexts)})
            # Remove raw snapshots from any interrupted earlier attempt.
            (directory / "raw.json").unlink(missing_ok=True)
            (directory / "report.md").write_text(markdown(doc))
            store.db.execute("DELETE FROM editions WHERE day=? AND status!='published'", (key,))
            store.db.commit()
            store.add(doc)
            build(settings, store)
            store.db.execute("UPDATE editions SET status='published' WHERE day=?", (key,))
            if notify:
                enqueue(settings, store, key)
            store.db.execute("UPDATE runs SET status='published',finished_at=? WHERE id=?", (utcnow().timestamp(), run_id))
            store.db.commit()
            result = {"state": "published", "edition": edition["id"], "date": day,
                      "articles": sum(len(s["articles"]) for s in doc["sections"]),
                      "url": settings.base_url + "/daily/" + key + "/"}
            if notify:
                result["notifications"] = deliver(settings, store)
            return result
    except BaseException as exc:
        store.db.execute("UPDATE runs SET status='failed',finished_at=?,error=? WHERE id=?",
                         (utcnow().timestamp(), type(exc).__name__, run_id))
        store.db.commit()
        raise
    finally:
        metrics.update({"cache_sources": len(cache), "model_calls": getattr(client, "usage", [])})
        store.db.execute("UPDATE runs SET metrics=? WHERE id=?", (json.dumps(metrics), run_id))
        store.db.commit()


def apply_repair(doc: dict, patch: dict, allowed: set[str]) -> dict:
    result = copy.deepcopy(doc)
    touched = set()
    for item in patch["replacements"]:
        key = item["event_key"]
        if key not in allowed or key in touched:
            raise ValueError("Repair changed an unrelated or repeated event")
        touched.add(key)
        section = next((s for s in result["sections"] if s["id"] == item["section_id"]), None)
        if section is None or not any(a["event_key"] == key for a in section["articles"]):
            raise ValueError("Repair moved or invented an event")
        replacement = item["article"]
        if replacement is not None and replacement["event_key"] != key:
            raise ValueError("Repair changed event identity")
        section["articles"] = [replacement if a["event_key"] == key else a for a in section["articles"]
                               if a["event_key"] != key or replacement is not None]
        if not section["articles"]:
            section["empty_reason"] = "候选未通过证据复核。"
    result["summary"] = patch["summary"]
    result["highlights"] = patch["highlights"]
    return result


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
                    store.db.execute("INSERT OR IGNORE INTO events(id,day,description) VALUES(?,?,?)", (description, day, description))
        store.db.execute("UPDATE editions SET status='published' WHERE day=?", (day,))
        store.db.commit()
        count += 1
    result = clean(settings, store)
    return {"imported": count, **result}
