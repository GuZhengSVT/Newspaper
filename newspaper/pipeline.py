from __future__ import annotations

import copy
import hashlib
import json
import re
import shutil
from subprocess import SubprocessError
import tempfile
from datetime import datetime, time, timedelta
from pathlib import Path

from jsonschema import ValidationError

from .config import TZ, Settings
from .bounded import research as bounded_research, supplement as bounded_supplement, SearchBudget, SearchPlanError
from .model import Model, ModelBudgetReached, ModelDeferred, provider_failure
from .coordinator import Coordinator
from .style import style_issues, compact_articles
from .evidence import selected_sources, used_evidence, validate_selection, context
from .events import editorial_history, link_report_events, same_article_event, update_coverage
from .notify import deliver, enqueue
from .publish import build, markdown
from .research import gather
from .retention import clean
from .storage import Store, atomic_json, normalize_entity, utcnow
from .translation import queue_translation, translate, translation_mode
from .validation import SCHEMA, validate_report, normalize


def generate(settings: Settings, store: Store, notify: bool = True, cutoff: datetime | None = None,
             *, manual: bool = False, task_id: str | None = None) -> dict:
    """Complete and deliver each board serially; board contexts and caches are isolated."""
    if cutoff is not None:
        now = utcnow().astimezone(settings.timezone)
        if cutoff.tzinfo is None:
            raise ValueError("Cutoff requires an explicit timezone")
        cutoff = cutoff.astimezone(settings.timezone)
        if cutoff.date() != now.date() or cutoff > now or (not settings.values.get('project') and cutoff.hour < 12):
            raise ValueError("Cutoff must be between today's noon and the current time")
    if not settings.sections:
        return {"state": "awaiting_sections", "date": utcnow().date().isoformat()}
    if settings.values.get('project') or 'schedule' in settings.values:
        from .schedule import jobs
        from .batches import prepare_batches, finish_batches
        results=[];cache={};board_state={};active_group=None
        scheduled=jobs(settings, cutoff or utcnow(),manual=True) if manual else jobs(settings,utcnow())
        if task_id:
            scheduled=[(e,w) for e,w in scheduled if e['task_id']==task_id]
            if not scheduled:raise ValueError('Task is unknown or not due; use --manual to run before its scheduled date')
        if cutoff is not None:
            scheduled=[(e,{**w,'window_end':cutoff.isoformat()} if w['date']==cutoff.date().isoformat() else w) for e,w in scheduled]
        if any(not store.published(w['date']+'/'+e['id']) for e,w in scheduled):
            migrate_publication_history(settings, store)
        if not manual:
            scheduled=[({**e,'id':key.split('/',1)[1]} if (key:=store.existing_publication(e,w)) else e,w)
                       for e,w in scheduled]
        batch_ids = None
        def prepare_notifications():
            nonlocal batch_ids
            if batch_ids is None:
                batch_ids = prepare_batches(settings, store, scheduled)
        notifications={'sent':0,'failed':0,'unconfigured':0,'uncertain':0} if notify else {}
        for edition, window in scheduled:
            key=window['date']+'/'+edition['id']
            if store.published(key):
                results.append({'edition': edition['id'], 'state': 'already_published', 'date': window['date']})
                continue
            now=utcnow().timestamp()
            failures=store.db.execute("select count(*),max(finished_at) from runs where day=? and status='failed' and started_at>?",
                                      (key,now-6*3600)).fetchone()
            if failures[0]>=3 or (failures[1] and now-failures[1]<300):
                results.append({'edition':edition['id'],'state':'retry_deferred','date':window['date']})
                continue
            try:
                if active_group!=edition['group_id']:
                    cache={};board_state={};active_group=edition['group_id']
                results.append(generate_edition(settings.for_edition(edition), store, edition, cache, False,
                                                publication_window=window, board_state=board_state,
                                                before_publish=prepare_notifications if notify else None,
                                                suppress_notification=not notify))
            except Exception as exc:
                results.append({'edition':edition['id'],'state':'failed','error_type':type(exc).__name__})
            if batch_ids and results[-1]['state'] == 'published':
                delivered=finish_batches(settings,store,batch_ids)
                for channel,count in delivered.items():notifications[channel]=notifications.get(channel,0)+count
        state=('partial_failure' if any(r['state']=='failed' for r in results) else
               'published' if any(r['state']=='published' for r in results) else 'idle')
        return {'state':state,'date':utcnow().date().isoformat(),'editions':results,**({'notifications':notifications} if notify else {})}
    if utcnow().hour < 12:
        return {"state": "before_scheduled_window", "date": utcnow().date().isoformat()}
    cache = {}
    results = []
    for edition in settings.editions:
        scoped = settings.for_edition(edition)
        try:
            results.append(generate_edition(scoped, store, edition, cache, notify, cutoff=cutoff,
                                            suppress_notification=not notify))
        except Exception as exc:
            results.append({"edition": edition["id"], "state": "failed", "error_type": type(exc).__name__})
            if not edition["id"]:  # Preserve the original single-edition CLI contract.
                raise
    if len(results) == 1 and not settings.editions[0]["id"]:
        return results[0]
    state = "published" if all(r["state"] in {"published", "already_published"} for r in results) else "partial_failure"
    return {"state": state, "date": utcnow().date().isoformat(), "editions": results}


def migrate_publication_history(settings: Settings, store: Store) -> None:
    """Reassign retained editions after a scheduling configuration change."""
    from .schedule import scope_publication_history

    spec = {key: settings.values.get(key) for key in ('project', 'schedule', 'editions', 'sections', 'publication_timezones')}
    signature = hashlib.sha256(json.dumps(spec, sort_keys=True, default=str).encode()).hexdigest()
    db = store.db
    if db.execute("SELECT 1 FROM sqlite_master WHERE name='publication_history_scope'").fetchone():
        row = db.execute('SELECT signature FROM publication_history_scope WHERE id=1').fetchone()
        if row and row[0] == signature:
            return
    scope_publication_history(settings, store)
    db.execute('CREATE TABLE IF NOT EXISTS publication_history_scope (id INTEGER PRIMARY KEY, signature TEXT NOT NULL)')
    db.execute('INSERT OR REPLACE INTO publication_history_scope VALUES(1,?)', (signature,))
    db.commit()


def generate_edition(settings: Settings, store: Store, edition: dict, cache: dict, notify: bool,
                     cutoff: datetime | None = None, publication_window: dict | None = None,
                     board_state: dict | None = None, before_publish=None, *, suppress_notification: bool = False) -> dict:
    now = utcnow()
    day = publication_window['date'] if publication_window else now.date().isoformat()
    key = day + ("/" + edition["id"] if edition["id"] else "")
    mode = translation_mode(settings)
    if store.published(key):
        return {"state": "already_published", "edition": edition["id"], "date": day}
    ready = store.db.execute("SELECT notify_suppressed FROM editions WHERE day=? AND status='ready'", (key,)).fetchone()
    if ready is not None:
        if suppress_notification and not ready['notify_suppressed']:
            store.db.execute('UPDATE editions SET notify_suppressed=1 WHERE day=?', (key,))
            store.db.commit()
        suppress_notification = suppress_notification or bool(ready['notify_suppressed'])
        doc = store.document(key)
        evidence = json.loads((settings.data / "editions" / key / "evidence.json").read_text())
        if (settings.values.get('search', {}).get('mode') == 'bounded' and not evidence['sources']
            and not evidence.get('collection_status', {}).get('fetched_sources')):
            raise ValueError('No verified originals in saved edition; refusing a no-news success')
        window = {k: doc[k] for k in ["date", "window_start", "window_end"]}
        validate_report(doc, settings, evidence["sources"], store.history(edition.get("task_id")), window)
        if mode == 'together':
            try:
                translate(doc, Model(settings, group_id=edition.get('group_id', edition['id']), store=store,
                                     edition_key=key),
                          on_article_translated=lambda article: atomic_json(settings.data / 'editions' / key / 'report.json', doc))
            except ModelBudgetReached as exc:
                return budget_deferred(edition, day, exc)
            except ModelDeferred:
                return {'state': 'retry_deferred', 'edition': edition['id'], 'date': day}
            atomic_json(settings.data / 'editions' / key / 'report.json',doc)
            (settings.data / 'editions' / key / 'report.md').write_text(markdown(doc,settings))
        if before_publish is not None:before_publish()
        build(store.settings, store)
        store.set_task(key,edition.get('task_id'))
        store.db.execute("UPDATE editions SET status='published',notify_suppressed=? WHERE day=?",
                         (int(suppress_notification), key))
        if mode == 'chinese_first':queue_translation(store, key)
        store.db.execute("INSERT INTO runs(day,started_at,finished_at,status,metrics) VALUES(?,?,?,'published',?)",
                         (key, now.timestamp(), utcnow().timestamp(), json.dumps({"resumed_from_ready": True})))
        if notify and not suppress_notification:enqueue(settings, store, key)
        store.db.commit()
        (settings.data / 'editions' / key / 'translation-checkpoint.json').unlink(missing_ok=True)
        return {"state": "published", "resumed": True, "edition": edition["id"], "date": day,
                "articles": sum(len(s["articles"]) for s in doc["sections"]),
                "url": settings.base_url + "/daily/" + key + "/",
                **({"notifications": deliver(settings, store)} if notify else {})}
    end = datetime.combine(now.date(), time(12), TZ)
    window = publication_window or {"date": day, "window_start": (end - timedelta(days=1)).isoformat(),
                                    "window_end": (cutoff or end).isoformat()}
    client = Model(settings, group_id=edition.get('group_id',edition['id']), store=store, edition_key=key)
    checkpoint_path = settings.data / 'editions' / key / 'translation-checkpoint.json'
    try:
        client.check_budget()
        client.check_backoff()
    except ModelBudgetReached as exc:
        if mode == 'together' or not checkpoint_path.is_file():
            return budget_deferred(edition, day, exc)
    except ModelDeferred:
        if mode == 'together' or not checkpoint_path.is_file():
            return {'state': 'retry_deferred', 'edition': edition['id'], 'date': day}
    cursor = store.db.execute("INSERT INTO runs(day,started_at,status) VALUES(?,?,'running')", (key, now.timestamp()))
    run_id = cursor.lastrowid
    store.db.commit()
    metrics = {"dsh_calls": 0, "dsh_tokens": None, "cache_sources": 0}
    def record_stage(stage):
        metrics.update(stage=stage, model_calls=client.usage)
        store.db.execute("UPDATE runs SET metrics=? WHERE id=?", (json.dumps(metrics), run_id))
        store.db.commit()
    client.on_stage = record_stage
    coordinator = None
    if edition['id']:
        coordinator = (board_state or {}).get('coordinator')
        if coordinator:coordinator.bind(settings,edition,window,client)
        else:
            coordinator = Coordinator(settings,edition,window,client)
            if board_state is not None:board_state['coordinator']=coordinator
    if coordinator and coordinator.model is not None:coordinator.model.on_stage = record_stage
    directory = settings.data / 'editions' / key

    def publish_document(doc, contexts, window, collection_status, *, related=None, candidates=None):
        history = store.history(edition.get('task_id'))
        validate_report(doc, settings, contexts, history, window)
        if coordinator:
            retained=coordinator.approve(doc,metrics)
            metrics['coordination']=coordinator.state
            if retained is not None and retained != {a['event_key'] for s in doc['sections'] for a in s['articles']}:
                doc=keep_approved(doc,{'article_reviews':[
                    {'event_key':a['event_key'],'ok':a['event_key'] in retained,'problems':[]}
                    for s in doc['sections'] for a in s['articles']]},
                    exclusion_reason='候选消息未达到本板块收录标准。',
                    exclusion_note='部分候选消息未达到本板块收录标准，未予收录。')
        if related is not None:
            link_report_events(doc, related, contexts, task_id=edition.get('task_id'), candidates=candidates)
        evidence = {'window': window, 'sources': used_evidence(doc, contexts),
                    'collection_status': collection_status}
        if mode == 'together':
            checkpoint = {'report': doc, 'evidence': evidence, 'notify_suppressed': suppress_notification,
                          'coordination': coordinator.state if coordinator else {}}
            atomic_json(checkpoint_path, checkpoint)
            metrics['translated_articles'] = translate(doc,client,
                diagnostics=metrics.setdefault('translation_failures', []),
                on_article_translated=lambda article: atomic_json(checkpoint_path, checkpoint))
        validate_report(doc, settings, contexts, history, window)
        if before_publish is not None:before_publish()
        directory.mkdir(parents=True, exist_ok=True)
        atomic_json(directory / 'report.json', doc)
        atomic_json(directory / 'evidence.json', evidence)
        (directory / 'raw.json').unlink(missing_ok=True)
        (directory / 'report.md').write_text(markdown(doc,settings))
        store.db.execute("DELETE FROM editions WHERE day=? AND status!='published'", (key,))
        store.db.commit()
        store.add(doc,task_id=edition.get('task_id'), suppress_notification=suppress_notification)
        record_stage('build')
        build(store.settings, store)
        update_coverage(metrics, doc, 'published')
        store.db.execute("UPDATE editions SET status='published',notify_suppressed=? WHERE day=?",
                         (int(suppress_notification), key))
        if mode == 'chinese_first':queue_translation(store, key)
        store.db.execute("UPDATE runs SET status='published',finished_at=? WHERE id=?", (utcnow().timestamp(), run_id))
        if notify and not suppress_notification:enqueue(settings, store, key)
        store.db.commit()
        checkpoint_path.unlink(missing_ok=True)
        result = {'state': 'published', 'edition': edition['id'], 'date': day,
                  'articles': sum(len(s['articles']) for s in doc['sections']),
                  'url': settings.base_url + '/daily/' + key + '/'}
        if notify:result['notifications'] = deliver(settings, store)
        return result

    try:
        if checkpoint_path.is_file():
            checkpoint = json.loads(checkpoint_path.read_text())
            evidence = checkpoint['evidence']
            if evidence['window'] == window:
                suppress_notification = suppress_notification or checkpoint.get('notify_suppressed', False)
                metrics['resumed_translation'] = True
                if coordinator:coordinator.state=checkpoint.get('coordination',{})
                return publish_document(checkpoint['report'], evidence['sources'], window, evidence['collection_status'])
        client.check_budget()
        client.check_backoff()
        if coordinator:
            coordinator.start()
            metrics['coordination']=coordinator.state
        with tempfile.TemporaryDirectory(dir=settings.data / "tmp", prefix="run-") as temp:
            work = Path(temp)
            collections = []
            coverage = []
            bounded = settings.values.get('search', {}).get('mode') == 'bounded'
            budget = SearchBudget(settings)
            if bounded:
                sources, candidates, coverage, _, search_metrics = bounded_research(settings, client, window, work, cache,
                                                                                  diagnostics=metrics, budget=budget)
                metrics.update(search_metrics, dsh_tokens=0)
                if not sources:
                    raise ValueError('No originals verified; refusing a no-news success')
                history = store.related_history(candidates, sources=sources, task_id=edition.get("task_id"), window=window)
                sources = store.reuse_event_evidence(candidates, sources, history, window=window)
                contexts = (selected_sources(sources, candidates) if candidates else
                            [{**s,'content':context(s['content'],[])} for s in sources])
            else:
                # Batches are explicitly configured related topics, never an arbitrary single mega-query.
                batches = {}
                for section in settings.sections:
                    batches.setdefault(section.get("batch", section["id"]), []).append(section)
                metrics['sections'] = {s['id']: {'indexes_read': 0, 'window_links': 0, 'originals_fetched': 0,
                                                  'verified': 0, 'published': 0} for s in settings.sections}
                for index, (name, sections) in enumerate(batches.items()):
                    group = sections[0] if len(sections) == 1 else {
                        "id": name, "name": "、".join(s["name"] for s in sections),
                        "scope": "\n".join(s["name"] + "：" + s.get("scope", "") for s in sections),
                        "sections": sections, "query_count": max(3, len(sections) * 2)}
                    remaining = min(budget.remaining_queries,
                                    group.get('query_count', settings.values.get('search', {}).get('queries_per_section', 3)))
                    if not remaining or len(budget.fetch_urls) >= budget.fetch_limit:
                        coverage.append(group['name'] + '的资料检索额度已用完，本期覆盖受限。')
                        continue
                    budget.take_queries(remaining)
                    group = {**group, 'query_count': remaining}
                    metrics["dsh_calls"] += 1
                    try:
                        collection = gather(settings, group, window, work / f"collect-{index}", cache=cache,
                                            budget=budget)
                        collections.append(collection)
                        for section in sections:
                            originals = budget.originals_by_section.setdefault(section['id'], set())
                            originals.update(s['url'] for s in collection['sources'])
                            metrics['sections'][section['id']]['originals_fetched'] = len(originals)
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
                    history = store.related_history(candidates, sources=sources, task_id=edition.get("task_id"), window=window)
                    sources = store.reuse_event_evidence(candidates, sources, history, window=window)
                    contexts = selected_sources(sources, candidates)
                else:
                    candidates = []
                    history = store.history(edition.get("task_id"))
                    contexts = sources
            feedback = {}
            repair_keys = None
            for index, candidate in enumerate(candidates, 1):
                candidate['candidate_id'] = 'c' + str(index)
            metrics['candidate_keys'] = [c['event_key'] for c in candidates]
            editor_schema = copy.deepcopy(SCHEMA)
            editor_schema['$defs']['article']['properties']['candidate_id'] = {'type': 'string'}
            if edition['id']:
                editor_schema['$defs']['article']['required'].append('candidate_id')
            validation_failures = 0
            review_rounds = 0
            length_repairs = 0
            evidence_repairs = 0
            for attempt in range(4):
                payload = {"window": window, "edition": edition, "sections": settings.sections,
                           "sources": contexts, "candidates": candidates,
                           "history": editorial_history(history), "schema": editor_schema, "feedback": feedback}
                if edition['id']:
                    payload['candidate_ids_by_section']={s['id']:[c['candidate_id'] for c in candidates
                        if c['section_id']==s['id']] for s in settings.sections}
                if attempt and repair_keys is not None and edition['id']:
                    if repair_keys:
                        scoped_candidates = [c for c in candidates if c['event_key'] in repair_keys]
                        repair_contexts = selected_sources(sources, scoped_candidates)
                        patch = request_repair(client, doc, repair_keys, repair_contexts,
                                               scoped_candidates, history, window, feedback)
                        doc = apply_repair(doc, patch, set(repair_keys))
                else:
                    doc = client.request("edit", payload)
                doc.update({"schema_version": 2 if edition["id"] else 1, **window,
                            "generated_at": utcnow().isoformat()})
                # Equivalent UTC/Japan/China timestamps describe the same instant.
                # Normalize before model review so offset spelling cannot reject news.
                for section in doc['sections']:
                    for article in section['articles']:
                        article.pop('image',None)  # Only the optional source-bound illustration stage may add images.
                        article.pop('bilingual',None)  # Only the independent translation stage may add English.
                        try:
                            moment = datetime.fromisoformat(article['published_at'])
                            if moment.tzinfo is not None:article['published_at'] = moment.astimezone(TZ).isoformat()
                        except (ValueError,TypeError):pass  # Schema/window validation handles invalid values.
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
                        restore_event_keys(doc, candidates)
                        link_report_events(doc, history, contexts, task_id=edition.get('task_id'), candidates=candidates)
                        metrics.setdefault("drafts", []).append({"attempt": attempt + 1,
                            "articles": sum(len(s["articles"]) for s in doc["sections"])})
                        original_draft = copy.deepcopy(doc)
                        rejected = []
                        doc = filter_invalid_articles(doc, settings, contexts, store.history(edition.get("task_id")), window,
                                                      {c["event_key"] for c in candidates},
                                                      diagnostics=rejected)
                        metrics.setdefault('article_rejections', []).extend(rejected)
                        repairable = [r for r in rejected if r['reason'] not in {
                            "Article is outside the edition's reporting window", 'Repeated event', 'Unselected event'}]
                        if repairable and evidence_repairs < 1:
                            evidence_repairs += 1
                            keys = {r['event_key'] for r in repairable}
                            scoped = [c for c in candidates if c['event_key'] in keys]
                            patch = request_repair(client, original_draft, keys, selected_sources(sources, scoped),
                                                   scoped, history, window, {'validation_errors': repairable})
                            repaired = apply_repair(original_draft, patch, keys)
                            link_report_events(repaired, history, contexts, task_id=edition.get('task_id'), candidates=candidates)
                            used = {e['source_id'] for s in repaired['sections'] for a in s['articles']
                                    for f in a['facts'] for e in f['evidence']}
                            repaired['sources'] = [{k: src[k] for k in ['id','url','title','published_at']}
                                                   for src in contexts if src['id'] in used]
                            remaining = []
                            doc = filter_invalid_articles(repaired, settings, contexts, store.history(edition.get("task_id")), window,
                                                          {c['event_key'] for c in candidates}, diagnostics=remaining)
                            metrics['evidence_repair'] = {'event_keys': sorted(keys), 'remaining_rejections': remaining}
                    else:
                        validate_report(doc, settings, contexts, history, window)
                    if edition["id"]:
                        length_problems = style_issues(doc, settings)
                        while length_problems and length_repairs < 2:
                            length_repairs += 1
                            keys = {p["event_key"] for p in length_problems}
                            patch = client.request("compress", {"articles": [
                                {"event_key": a['event_key'],"title": a['title'],
                                 "facts": [{"index": i,"text": f['text']} for i,f in enumerate(a['facts'])],
                                 "max_characters": int(next(p['max_characters'] for p in length_problems if p['event_key']==a['event_key']) * 0.8)}
                                for s in doc['sections'] for a in s['articles'] if a['event_key'] in keys]})
                            doc = compact_articles(doc, patch, keys)
                            metrics.setdefault('length_checks', []).append({'before': length_problems,'after': style_issues(doc, settings)})
                            length_problems = style_issues(doc, settings)
                        oversized = {p["event_key"] for p in style_issues(doc, settings)}
                        doc = filter_invalid_articles(doc, settings, contexts, store.history(edition.get("task_id")), window,
                                                      {c["event_key"] for c in candidates},
                                                      diagnostics=metrics.setdefault("article_rejections", []),
                                                      oversized=oversized)
                        planned = {c["event_key"] for c in candidates}
                        if any(a["event_key"] not in planned for sec in doc["sections"] for a in sec["articles"]):
                            raise ValueError("Unselected event in report")
                        all_keys = {normalize(e["id"]) for e in store.history(edition.get("task_id"))}
                        if any(normalize(a["event_key"]) in all_keys and not a["follow_up"]
                               for sec in doc["sections"] for a in sec["articles"]):
                            raise ValueError("Previously published event")
                except (ValueError, ValidationError) as exc:
                    feedback = {"validation_error": str(exc)[:300]}
                    metrics.setdefault('validation_errors', []).append(type(exc).__name__ + ': ' +
                        (str(exc.validator) if isinstance(exc, ValidationError) else str(exc)[:180]))
                    validation_failures += 1
                    if validation_failures == 1:
                        repair_keys = None
                        continue
                    raise
                evidence = used_evidence(doc, contexts)
                draft_keys={a['event_key'] for s in doc['sections'] for a in s['articles']}
                if not draft_keys:evidence=contexts
                review_payload={"window": window, "sections": settings.sections,
                    "report": doc, "sources": evidence, "history": editorial_history(history),
                    "candidates": [c for c in candidates if c['event_key'] in draft_keys],
                    "expected_event_keys": sorted(draft_keys),
                    "collection_status": {"any_original_fetched": bool(sources), "coverage_notes": coverage}}
                for contract_attempt in range(2):
                    review=client.request('review',review_payload)
                    if not edition['id']:break
                    try:keep_approved(doc,review)
                    except ValueError:
                        if contract_attempt:raise
                        metrics['review_contract_retries']=metrics.get('review_contract_retries',0)+1
                        review_payload={**review_payload,'validation_error':
                            'article_reviews must cover expected_event_keys exactly once; an empty report requires article_reviews=[]'}
                        continue
                    break
                metrics.setdefault("reviews", []).append({"attempt": attempt + 1,"ok": review.get("ok"),
                    "problems": [str(p)[:200] for p in review.get('problems', [])[:5]],
                    "research_queries": len(review.get('research_queries', [])),
                    "articles": [{"event_key": r.get("event_key"),"ok": r.get("ok"),
                                  "problems": [str(p)[:200] for p in r.get("problems", [])[:3]]}
                                 for r in review.get("article_reviews", [])]})
                review_rounds += 1
                if (review.get("ok") is True and not review.get("problems") and not review.get("research_queries")
                    and (not edition["id"] or review.get("summary_ok") is True)):
                    if edition["id"]:
                        checked = keep_approved(doc, review)
                        if sum(len(s["articles"]) for s in checked["sections"]) != sum(len(s["articles"]) for s in doc["sections"]):
                            raise ValueError("Contradictory final review verdicts")
                    break
                approved = any(r.get('ok') is True and not r.get('problems')
                               for r in review.get('article_reviews', []))
                # When every article needs a correction, allow one final scoped
                # repair rather than turning fixable drafts into an empty failure.
                if review_rounds >= 2 and (not edition['id'] or approved or review_rounds >= 3
                                          or not any(s['articles'] for s in doc['sections'])):
                    if edition["id"]:
                        doc = keep_approved(doc, review)
                        if not any(s['articles'] for s in doc['sections']):
                            raise ValueError('Final review did not approve any articles or an empty edition')
                        validate_report(doc, settings, contexts, history, window)
                        break
                    raise ValueError("Evidence review rejected the report")
                feedback = review
                existing = {a["event_key"] for sec in doc["sections"] for a in sec["articles"]}
                affected = review.get("affected_event_keys", [])
                verdicts = review.get('article_reviews', [])
                if (existing and {r.get('event_key') for r in verdicts} == existing and len(verdicts) == len(existing)):
                    repair_keys = [r['event_key'] for r in verdicts if r.get('ok') is not True or r.get('problems')]
                    if not repair_keys and not review.get('research_queries'):
                        # Count/title summaries can be replaced without rewriting approved facts.
                        doc = keep_approved(doc, review)
                        validate_report(doc, settings, contexts, history, window)
                        break
                else:
                    repair_keys = [k for k in affected if k in existing] or list(existing)
                by_id = {s["id"]: s for s in settings.sections}
                for index, query in enumerate(review.get("research_queries", [])[:3]):
                    if (isinstance(query, dict) and query.get('section_id') in by_id
                            and isinstance(query.get('query'), str) and query['query'].strip()):
                        section = by_id[query['section_id']]
                        if budget.remaining_queries <= 0 or len(budget.fetch_urls) >= budget.fetch_limit:
                            coverage.append(section['name'] + '的补查额度已用完，本期覆盖受限。')
                            continue
                        try:
                            extra_sources = bounded_supplement(settings, section, query['query'],
                                                              work / f'supplement-{attempt}-{index}', cache, window,
                                                              budget=budget, diagnostics=metrics, identify=bounded)
                        except (SubprocessError, ValueError) as exc:
                            metrics.setdefault('supplement_failures', []).append({'section_id': section['id'],
                                                                               'error_type': type(exc).__name__})
                            coverage.append(section['name'] + '的补查未完成，本期覆盖受限。')
                            continue
                        sources = list({s['id']: s for s in [*sources, *extra_sources]}.values())
                        # Supplement sources serve existing candidates only.
                        for candidate in candidates:
                            if (candidate['section_id'] == section['id']
                                    and (not repair_keys or candidate['event_key'] in repair_keys)):
                                candidate['source_ids'] = list(dict.fromkeys(candidate['source_ids'] + [s['id'] for s in extra_sources]))
                        if edition['id']:
                            contexts = (selected_sources(sources, candidates) if candidates else
                                        [{**s, 'content': context(s['content'], [])} for s in sources])
                        else:
                            contexts = sources
            else:
                raise ValueError('Report exhausted bounded validation and review retries')
            from .images import illustrate
            metrics.update(search_query_limit=budget.query_limit, fetch_attempts=len(budget.fetch_urls))
            update_coverage(metrics, doc, 'verified')
            illustrate(doc, contexts, client)
            return publish_document(doc, contexts, window,
                                    {'fetched_sources': sum(not s.get('prior_edition') for s in sources)},
                                    related=history if edition['id'] else None, candidates=candidates)
    except ModelBudgetReached as exc:
        metrics['model_budget'] = {'total_tokens': exc.total_tokens, 'limit': exc.limit}
        store.db.execute("UPDATE runs SET status='deferred',finished_at=? WHERE id=?", (utcnow().timestamp(), run_id))
        store.db.commit()
        return budget_deferred(edition, day, exc)
    except ModelDeferred:
        store.db.execute("UPDATE runs SET status='deferred',finished_at=? WHERE id=?", (utcnow().timestamp(), run_id))
        store.db.commit()
        return {'state': 'retry_deferred', 'edition': edition['id'], 'date': day}
    except BaseException as exc:
        if isinstance(exc,SearchPlanError):metrics['search_plan_errors']=exc.problems
        status = 'provider_failed' if provider_failure(exc) else 'failed'
        store.db.execute('UPDATE runs SET status=?,finished_at=?,error=? WHERE id=?',
                         (status, utcnow().timestamp(), type(exc).__name__, run_id))
        store.db.commit()
        raise
    finally:
        metrics.update({"cache_sources": len(cache), "model_calls": getattr(client, "usage", [])})
        store.db.execute("UPDATE runs SET metrics=? WHERE id=?", (json.dumps(metrics), run_id))
        store.db.commit()


def budget_deferred(edition, day, error):
    return {'state': 'retry_deferred', 'edition': edition['id'], 'date': day, 'reason': 'model_token_budget',
            'total_tokens': error.total_tokens, 'limit': error.limit}


def restore_event_keys(doc: dict, candidates: list[dict]) -> None:
    """Bind drafts to the selected event by a short ID; never rewrite article facts."""
    selected = {c['candidate_id']: c for c in candidates if c.get('candidate_id')}
    for section in doc['sections']:
        for article in section['articles']:
            reference = article.pop('candidate_id', None)
            if reference is None:continue
            candidate = selected.get(reference)
            if candidate is None:
                raise ValueError(f'Unknown candidate reference {reference!r}; use candidate_ids_by_section')
            if candidate['section_id'] != section['id']:
                raise ValueError(f'Candidate {reference!r} belongs to section {candidate["section_id"]!r}; '
                                 f'move it from {section["id"]!r} to that section or omit it')
            article['event_key'] = candidate['event_key']


def request_repair(client, doc, keys, sources, candidates, history, window, feedback):
    keys = set(keys)
    scoped_sections = []
    for section in doc['sections']:
        articles = [a for a in section['articles'] if a['event_key'] in keys]
        if articles:
            scoped_sections.append({'id': section['id'], 'name': section['name'], 'articles': articles})
    ids = {s['id'] for s in scoped_sections}
    entities = {normalize_entity(e) for c in candidates for e in c.get('entities', [])} - {''}
    referenced = {(link.get('edition'), link.get('event_key'))
                  for section in scoped_sections for article in section['articles']
                  for link in article.get('event_links', [])}
    scoped_history = [row for row in history if row['id'] in keys or (row.get('day'), row['id']) in referenced
                      or entities.intersection(normalize_entity(e) for e in row.get('entities', []))]
    scoped_feedback = {key: [row for row in feedback.get(key, []) if row.get('event_key') in keys]
                       for key in ('article_reviews', 'validation_errors')}
    if not any(scoped_feedback.values()):
        scoped_feedback['problems'] = [str(problem)[:200] for problem in feedback.get('problems', [])[:5]]
    return client.request('repair', {'window': window, 'report': {'sections': scoped_sections},
        'sections': [s for s in client.settings.sections if s['id'] in ids], 'event_keys': sorted(keys),
        'sources': sources, 'history': editorial_history(scoped_history), 'feedback': scoped_feedback,
        'schema': SCHEMA['$defs']})


def filter_invalid_articles(doc: dict, settings: Settings, sources: list[dict], history: list[dict],
                            window: dict, allowed: set[str], diagnostics: list | None = None,
                            oversized: set[str] | None = None) -> dict:
    """A bad quotation excludes its article, never weakens evidence requirements."""
    base = copy.deepcopy(doc)
    for section in base["sections"]:
        section["articles"] = []
        section["empty_reason"] = "候选核验中。"
    validate_report(base, settings, sources, history, window)
    result = copy.deepcopy(doc)
    accepted = []
    source_urls = {source['id']: source['url'] for source in sources}
    rejected = False
    for index, section in enumerate(result["sections"]):
        kept = []
        for article in section["articles"]:
            try:
                single = copy.deepcopy(base)
                single["sections"][index]["articles"] = [article]
                validate_report(single, settings, sources, history, window)
                if article['event_key'] in (oversized or set()):
                    raise ValueError('Article exceeds public length limit')
                if article["event_key"] not in allowed:
                    raise ValueError("Unselected event")
                if any(same_article_event(article, previous, source_urls) for previous in accepted):
                    raise ValueError('Repeated event')
            except (ValueError, ValidationError) as exc:
                if diagnostics is not None:
                    reason = ('schema:' + str(exc.validator) + ':' + '/'.join(map(str, exc.absolute_path))
                              if isinstance(exc, ValidationError) else str(exc)[:180])
                    diagnostics.append({"event_key": article.get("event_key"), "reason": reason})
                rejected = True
                continue
            accepted.append(article)
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


def keep_approved(doc: dict, review: dict, *, exclusion_reason='候选消息未达到证据核验标准。',
                  exclusion_note='部分候选消息未能充分核实，未予收录。') -> dict:
    """Publish only individual articles the final review explicitly approved."""
    expected = {a["event_key"] for s in doc["sections"] for a in s["articles"]}
    verdicts = review.get("article_reviews", [])
    if (not isinstance(verdicts, list) or len(verdicts) != len(expected)
        or any(not isinstance(r,dict) or not isinstance(r.get('event_key'),str) for r in verdicts)
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
            section["empty_reason"] = exclusion_reason
    articles = [a for s in result["sections"] for a in s["articles"]]
    # Deterministic replacements cannot introduce new factual claims.
    result["highlights"] = [a["title"] for a in articles[:5]]
    result["summary"] = ("本期收录" + str(len(articles)) + "条通过核验的新闻。" if articles else
                         "本期未收录新闻：" + exclusion_reason)
    used = {e["source_id"] for a in articles for f in a["facts"] for e in f["evidence"]}
    result["sources"] = [s for s in result["sources"] if s["id"] in used]
    result["coverage_notes"] = list(dict.fromkeys(result.get("coverage_notes", []) +
        ([exclusion_note] if approved != expected else [])))[:30]
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
        replacement = copy.deepcopy(item["article"])
        if replacement is not None:
            replacement['event_key'] = key
        section["articles"] = [replacement if a["event_key"] == key else a for a in section["articles"]
                               if a["event_key"] != key or replacement is not None]
        if section["articles"]:
            section["empty_reason"] = ""
        else:
            section["empty_reason"] = "候选未通过证据复核。"
    articles = [a for section in result['sections'] for a in section['articles']]
    result['summary'] = (f'本期收录{len(articles)}条新闻。' if articles else '本期未收录新闻：候选消息未达到证据核验标准。')
    result['highlights'] = [a['title'] for a in articles[:5]]
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
