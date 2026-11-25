from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

from .config import CONFIG_VERSION, ROOT, ConfigError, Settings, release_info
from .notify import deliver, readiness
from .pipeline import generate, import_legacy
from .publish import build
from .retention import clean, clean_daily
from .storage import Store, lock
from .alerts import COMMANDS, record_command, process_failure_alerts


def section_metrics(settings: Settings, store: Store) -> dict:
    remaining = {section["id"] for section in settings.sections}
    result = {}
    keys = ("indexes_read", "window_links", "originals_fetched", "verified", "published")
    for row in store.db.execute(
        """SELECT day,finished_at,metrics FROM runs WHERE status='published' AND finished_at IS NOT NULL
           ORDER BY finished_at DESC,id DESC"""
    ):
        if not remaining:
            break
        try:
            metrics = json.loads(row["metrics"])
        except (ValueError, TypeError):
            continue
        sections = metrics.get("sections") if isinstance(metrics, dict) else None
        if not isinstance(sections, dict):
            continue
        for section_id in sorted(remaining.intersection(sections)):
            counts = sections[section_id]
            if not isinstance(counts, dict) or not all(
                type(counts.get(key)) is int and counts[key] >= 0 for key in keys
            ):
                continue
            result[section_id] = {"publication": row["day"],
                                  "sampled_at": datetime.fromtimestamp(row["finished_at"], timezone.utc).isoformat(),
                                  **{key: counts[key] for key in keys}}
            remaining.remove(section_id)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Newspaper daily publishing pipeline")
    parser.add_argument("--config")
    release = release_info()
    parser.add_argument("--version", action="version",
                        version=f"newspaper {release['version']} ({release['commit'] or 'source checkout'})")
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run", help="Generate and publish to the configured site")
    run.add_argument("--no-notify", action="store_true", help="Publish to the configured site without notifications")
    run.add_argument("--daily-clean", action="store_true", help="Start at local noon and complete cleanup once before production")
    preview = commands.add_parser("preview", help="Generate into a new private directory without publishing or notifying")
    preview.add_argument("--output", type=Path, required=True, help="New directory for preview data and website")
    for command in (run, preview):
        command.add_argument('--manual',action='store_true',help='Run enabled schedules immediately, with separate manual publication IDs')
        command.add_argument('--task',help='Run only the task ID printed by schedule')
        command.add_argument("--cutoff", type=datetime.fromisoformat,
                             help="Explicit timezone-aware cutoff for today's issue; defaults to noon")
    for cmd in ["build", "clean", "notify", "publish-site", "status", "doctor", "schedule", "check-config",
                "settings", "accounts", "translate"]:
        command_parser=commands.add_parser(cmd)
        if cmd=='schedule':command_parser.add_argument('--manual',action='store_true',help='List all enabled tasks, including future schedules')
        if cmd == "translate":
            command_parser.add_argument("--pending", action="store_true", help="Continue pending Chinese-first translations")
    legacy = commands.add_parser("import-legacy")
    legacy.add_argument("directory", type=Path)
    args = parser.parse_args()
    try:
        settings = Settings.load(args.config)
        if args.command == "settings":
            print(json.dumps(settings.effective(), ensure_ascii=False))
            return 0
        if args.command == "publish-site":
            from .publication import sync_pages
            result = sync_pages(settings)
            print(json.dumps(result, ensure_ascii=False))
            return 1 if result.get("state") in {"failed", "partial_failure"} else 0
        if args.command == "preview":
            settings = settings.for_preview(args.output)
            with lock(settings):
                store = Store(settings)
                try:
                    result = generate(settings, store, notify=False, cutoff=args.cutoff,
                                      manual=args.manual, task_id=args.task)
                    if not (settings.web / "current").exists():
                        build(settings, store)
                finally:
                    store.close()
            print(json.dumps({**result, "preview_site": str(settings.web / "current")}, ensure_ascii=False))
            return 1 if result.get("state") == "partial_failure" else 0
        if args.command=='accounts':
            from .accounts import accounts, account_mode
            # Addresses stay private even in command output and service logs.
            print(json.dumps({'mode':'accounts' if account_mode(settings) else 'legacy','accounts':[
                {'id':a['id'],'name':a.get('name',a['id']),'email_count':len(a['emails']),
                 'telegram_bound':bool(a['telegram_chat_id']),'subscriptions':a['options']}
                for a in accounts(settings)]},ensure_ascii=False));return 0
        if args.command in {'schedule','check-config'}:
            from .schedule import jobs
            from .storage import utcnow
            result={'valid':True,'groups':len(settings.editions),'sections':len(settings.sections),
                    'config_version':CONFIG_VERSION}
            if args.command=='schedule':
                scheduled=jobs(settings,utcnow(),manual=True) if args.manual else jobs(settings,utcnow());tasks={}
                result['jobs']=[{'id':e['id'],'task_id':e['task_id'],'name':e['name'],'sections':e['sections'],
                    'schedule':e['schedule'],'notify':e['notify'],**w} for e,w in scheduled]
                for job,window in scheduled:
                    item=tasks.setdefault(job['task_id'],{'id':job['task_id'],'schedule':job['schedule'],**window,'publications':[]})
                    item['publications'].append(job['id'])
                result['tasks']=list(tasks.values())
            print(json.dumps(result,ensure_ascii=False));return 0
        if args.command == "doctor":
            result = {"node": shutil.which("node") is not None,
                      "dsh_installed": (ROOT / "node_modules/.bin/dsh").is_file(),
                      "model_configured": bool(os.environ.get(settings.values.get("model", {}).get("api_key_env", "NEWSPAPER_API_KEY"))),
                      "sections": len(settings.sections),
                      "email_configured": readiness("email"), "telegram_configured": readiness("telegram"),
                      "base_url": settings.base_url}
            print(json.dumps(result, ensure_ascii=False))
            return 0 if result["node"] and result["dsh_installed"] and result["model_configured"] else 1
        clean_day = None
        if args.command == "run" and args.daily_clean:
            now = datetime.now(settings.timezone)
            if now.hour < 12:
                print(json.dumps({"state": "before_scheduled_window", "date": now.date().isoformat()}))
                return 0
            clean_day = now.date()
        # The daily run must not be lost when a maintenance job holds the lock.
        with lock(settings, wait_seconds=90 if args.command == "run" else 0):
            store = Store(settings)
            try:
                if args.command == "run":
                    if clean_day is not None:
                        clean_daily(settings, store, clean_day)
                    result = generate(settings, store, notify=not args.no_notify, cutoff=args.cutoff,
                                      manual=args.manual,task_id=args.task)
                elif args.command == "build":
                    result = build(settings, store)
                elif args.command == "clean":
                    result = clean(settings, store)
                elif args.command == "notify":
                    result = deliver(settings, store)
                elif args.command == 'translate':
                    from .translation import resume_translations
                    result = resume_translations(settings, store, manual=not args.pending)
                    if result['failed']:
                        result['state'] = 'partial_failure'
                elif args.command == "import-legacy":
                    result = import_legacy(settings, store, args.directory)
                else:
                    from .publication import status
                    from .model import model_usage
                    result = {"published": len(store.editions()), "sections": len(settings.sections),
                              "publication": status(settings), "release": release, "config_version": CONFIG_VERSION,
                              "section_metrics": section_metrics(settings, store),
                              "model_token_usage": model_usage(settings, store),
                              "notifications": [dict(r) for r in store.db.execute("SELECT channel,state,count(*) AS count FROM deliveries GROUP BY channel,state")],
                              "task_notifications": [dict(r) for r in store.db.execute("SELECT batch_id,channel,state,attempts,last_error FROM notification_outbox")],
                              "account_notifications": [dict(r) for r in store.db.execute("SELECT batch_id,account_id,channel,state,attempts,progress,last_error FROM account_outbox")],
                              "failure_alerts": [dict(r) for r in store.db.execute("SELECT incident_id,account_id,channel,state,attempts,last_error FROM failure_alert_outbox")],
                              "tasks": [dict(r) for r in store.db.execute("SELECT id,period,window_end,state FROM notification_batches")]}
            except Exception as exc:
                if args.command in COMMANDS and not (args.command == 'run' and args.no_notify):
                    record_command(settings,store,args.command,exc)
                    process_failure_alerts(settings,store)
                raise
            else:
                if args.command in COMMANDS and not (args.command == 'run' and args.no_notify):
                    record_command(settings,store,args.command)
                    alerts=process_failure_alerts(settings,store)
                    if settings.values.get('notifications',{}).get('failure_alerts'):
                        result['failure_alerts']=alerts
            finally:
                store.close()
        print(json.dumps(result, ensure_ascii=False))
        return 1 if result.get("state") == "partial_failure" else 0
    except BlockingIOError:
        print('{"state":"busy"}')
        return 75
    except ConfigError as exc:
        print(json.dumps({"state": "failed", "error_type": type(exc).__name__, "error": str(exc)}))
        return 1
    except Exception as exc:
        print(json.dumps({"state": "failed", "error_type": type(exc).__name__}))
        return 1


if __name__ == "__main__":
    sys.exit(main())
