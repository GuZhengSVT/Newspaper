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
from .style import style_issues
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
    if store.db.execute("SELECT 1 FROM editions WHERE day=? AND status='ready'", (key,)).fetchone():
        doc = store.document(key)
        evidence = json.loads((settings.data / "editions" / key / "evidence.json").read_text())
        window = {k: doc[k] for k in ["date", "window_start", "window_end"]}
        validate_report(doc, settings, evidence["sources"], store.history(), window)
        build(settings, store)
        store.db.execute("UPDATE editions SET status='published' WHERE day=?", (key,))
        store.db.execute("INSERT INTO runs(day,started_at,finished_at,status,metrics) VALUES(?,?,?,'published',?)",
                         (key, now.timestamp(), utcnow().timestamp(), json.dumps({"resumed_from_ready": True})))
        store.db.commit()
        if notify:
            enqueue(settings, store, key)
        return {"state": "published", "resumed": True, "edition": edition["id"], "date": day,
                "articles": sum(len(s["articles"]) for s in doc["sections"]),
                "url": settings.base_url + "/daily/" + key + "/",
                **({"notifications": deliver(settings, store)} if notify else {})}
    end = datetime.combine(now.date(), time(12), TZ)
    window = {"date": day, "window_start": (end - timedelta(days=1)).isoformat(), "window_end": end.isoformat()}
    cursor = store.db.execute("INSERT INTO runs(day,started_at,status) VALUES(?,?,'running')", (key, now.timestamp()))
    run_id = cursor.lastrowid
    store.db.commit()
    client = Model(settings)
    metrics = {"dsh_calls": 0, "dsh_tokens": None, "cache_sources": 0}
    def record_stage(stage):
        metrics.update(stage=stage, model_calls=client.usage)
        store.db.execute("UPDATE runs SET metrics=? WHERE id=?", (json.dumps(metrics), run_id))
        store.db.commit()
    client.on_stage = record_stage
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
                        coverage.append(group["name"] + "的部分资料检索未完成，本期覆盖受限。")
                sources = list({s["id"]: s for c in collections for s in c["sources"]}.values())
                if not sources:
                    raise ValueError("No fetched source material; refusing to publish an empty success")
                if edition["id"]:
                    limit = settings.values.get("search", {}).get("max_candidates_per_edition", 12)
                    selection = client.request("select", {"window": window, "sections": settings.sections,
                        "sources": [{**s, "content": s["content"][:2200]} for s in sources],
                        "candidate_limit": limit})
                    candidates = validate_selection(selection, settings, sources, limit)
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
                        "sources": repair_contexts, "history": history, "feedback": feedback, "schema": SCHEMA})
                    doc = apply_repair(doc, patch, set(repair_keys))
                else:
                    doc = client.request("edit", payload)
                doc.update({"schema_version": 2 if edition["id"] else 1, **window,
                            "generated_at": utcnow().isoformat()})
                if edition["id"]:
                    doc["edition"] = edition["id"]
                    doc["title"] = day + " " + edition["name"]
                    doc["coverage_notes"] = list(dict.fromkeys(coverage))[:5]
                # Only references actually cited in the draft enter the public report.
                used = {e["source_id"] for section in doc["sections"] for article in section["articles"]
                        for fact in article["facts"] for e in fact["evidence"]}
                doc["sources"] = [{k: src[k] for k in ["id", "url", "title", "published_at"]}
                                  for src in contexts if src["id"] in used]
                try:
                    if edition["id"]:
                        doc = filter_invalid_articles(doc, settings, contexts, store.history(), window,
                                                      {c["event_key"] for c in candidates})
                    else:
                        validate_report(doc, settings, contexts, history, window)
                    if edition["id"]:
                        length_problems = style_issues(doc)
                        if length_problems and attempt == 0:
                            keys = {p["event_key"] for p in length_problems}
                            shortened = copy.deepcopy(doc)
                            for sec in shortened["sections"]:
                                sec["articles"] = [a for a in sec["articles"] if a["event_key"] in keys]
                            patch = client.request("repair", {"window": window, "report": shortened,
                                "event_keys": list(keys), "sources": used_evidence(shortened, contexts), "schema": SCHEMA,
                                "feedback": {"length_limits": length_problems,
                                    "problems": ["压缩超长正文，保留核心事实及必要条件，删除宣传数据、无关背景和重复说明。不要用删除整条代替缩写。"]}})
                            doc = apply_repair(doc, patch, keys)
                        oversized = {p["event_key"] for p in style_issues(doc)}
                        doc = filter_invalid_articles(doc, settings, contexts, store.history(), window,
                                                      {c["event_key"] for c in candidates} - oversized)
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
                    "collection_status": {"any_original_fetched": bool(sources), "coverage_notes": coverage}})
                if (review.get("ok") is True and not review.get("problems") and not review.get("research_queries")
                    and (not edition["id"] or review.get("summary_ok") is True)):
                    if edition["id"]:
                        checked = keep_approved(doc, review)
                        if sum(len(s["articles"]) for s in checked["sections"]) != sum(len(s["articles"]) for s in doc["sections"]):
                            raise ValueError("Contradictory final review verdicts")
                    break
                if attempt == 1:
                    if edition["id"]:
                        doc = keep_approved(doc, review)
                        validate_report(doc, settings, contexts, history, window)
                        break
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


def filter_invalid_articles(doc: dict, settings: Settings, sources: list[dict], history: list[dict],
                            window: dict, allowed: set[str]) -> dict:
    """A bad quotation excludes its article, never weakens evidence requirements."""
    base = copy.deepcopy(doc)
    for section in base["sections"]:
        section["articles"] = []
        section["empty_reason"] = "候选核验中。"
    validate_report(base, settings, sources, history, window)
    result = copy.deepcopy(doc)
    seen = set()
    rejected = False
    for index, section in enumerate(result["sections"]):
        kept = []
        for article in section["articles"]:
            try:
                single = copy.deepcopy(base)
                single["sections"][index]["articles"] = [article]
                validate_report(single, settings, sources, history, window)
                if article["event_key"] not in allowed or normalize(article["event_key"]) in seen:
                    raise ValueError("Unselected or repeated event")
            except (ValueError, ValidationError):
                rejected = True
                continue
            seen.add(normalize(article["event_key"]))
            kept.append(article)
        section["articles"] = kept
        if kept:
            section["empty_reason"] = ""
        if not kept:
            section["empty_reason"] = "候选消息未达到证据核验标准。"
    if rejected:
        articles = [a for s in result["sections"] for a in s["articles"]]
        result["summary"] = ("本期收录" + str(len(articles)) + "条新闻。" if articles else
                             "本期未收录新闻：候选消息未达到证据核验标准。")
        result["highlights"] = [a["title"] for a in articles[:5]]
        result["coverage_notes"] = list(dict.fromkeys(result.get("coverage_notes", []) +
            ["部分候选资料未能充分核实，未予收录。"]))[:5]
    used = {e["source_id"] for sec in result["sections"] for a in sec["articles"] for f in a["facts"] for e in f["evidence"]}
    result["sources"] = [s for s in result["sources"] if s["id"] in used]
    validate_report(result, settings, sources, history, window)
    return result


def keep_approved(doc: dict, review: dict) -> dict:
    """Publish only individual articles the final review explicitly approved."""
    expected = {a["event_key"] for s in doc["sections"] for a in s["articles"]}
    verdicts = review.get("article_reviews", [])
    if (not isinstance(verdicts, list) or len(verdicts) != len(expected)
        or {r.get("event_key") for r in verdicts} != expected
        or any(not isinstance(r.get("ok"), bool) or not isinstance(r.get("problems"), list) for r in verdicts)):
        raise ValueError("Final review did not cover every article")
    approved = {r["event_key"] for r in verdicts if r["ok"] is True and not r["problems"]}
    result = copy.deepcopy(doc)
    for section in result["sections"]:
        section["articles"] = [a for a in section["articles"] if a["event_key"] in approved]
        if section["articles"]:
            section["empty_reason"] = ""
        else:
            section["empty_reason"] = "候选消息未达到证据核验标准。"
    articles = [a for s in result["sections"] for a in s["articles"]]
    # Deterministic replacements cannot introduce new factual claims.
    result["highlights"] = [a["title"] for a in articles[:5]]
    result["summary"] = ("本期收录" + str(len(articles)) + "条通过核验的新闻。" if articles else
                         "本期未收录新闻：候选消息未达到证据核验标准。")
    used = {e["source_id"] for a in articles for f in a["facts"] for e in f["evidence"]}
    result["sources"] = [s for s in result["sources"] if s["id"] in used]
    result["coverage_notes"] = list(dict.fromkeys(result.get("coverage_notes", []) +
        (["部分候选消息未能充分核实，未予收录。"] if approved != expected else [])))[:30]
    return result


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
        if section["articles"]:
            section["empty_reason"] = ""
        else:
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
