from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
from datetime import datetime
from pathlib import Path

from .config import ROOT, Settings
from .notify import deliver, readiness
from .pipeline import generate, import_legacy
from .publish import build
from .retention import clean
from .storage import Store, lock


def main() -> int:
    parser = argparse.ArgumentParser(description="Newspaper daily publishing pipeline")
    parser.add_argument("--config")
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run")
    run.add_argument("--no-notify", action="store_true", help="Publish a preview without enqueueing email or Telegram")
    run.add_argument('--manual',action='store_true',help='Run enabled schedules immediately, with separate manual publication IDs')
    run.add_argument('--task',help='Run only the task ID printed by schedule')
    run.add_argument("--cutoff", type=datetime.fromisoformat,
                     help="Explicit timezone-aware cutoff for today's issue; defaults to noon")
    for cmd in [ "build", "clean", "notify", "status", "doctor", "schedule", "check-config", "accounts", "translate"]:
        command_parser=commands.add_parser(cmd)
        if cmd=='schedule':command_parser.add_argument('--manual',action='store_true',help='List all enabled tasks, including future schedules')
    legacy = commands.add_parser("import-legacy")
    legacy.add_argument("directory", type=Path)
    args = parser.parse_args()
    try:
        settings = Settings.load(args.config)
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
            result={'valid':True,'groups':len(settings.editions),'sections':len(settings.sections)}
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
                      "model_configured": bool(os.environ.get(settings.values["model"].get("api_key_env", "NEWSPAPER_API_KEY"))),
                      "sections": len(settings.sections),
                      "email_configured": readiness("email"), "telegram_configured": readiness("telegram"),
                      "base_url": settings.base_url}
            print(json.dumps(result, ensure_ascii=False))
            return 0 if result["node"] and result["dsh_installed"] and result["model_configured"] else 1
        # The daily run must not be lost when the noon cleanup starts first.
        with lock(settings, wait_seconds=90 if args.command == "run" else 0):
            store = Store(settings)
            try:
                if args.command == "run":
                    result = generate(settings, store, notify=not args.no_notify, cutoff=args.cutoff,
                                      manual=args.manual,task_id=args.task)
                elif args.command == "build":
                    result = build(settings, store)
                elif args.command == "clean":
                    result = clean(settings, store)
                elif args.command == "notify":
                    result = deliver(settings, store)
                elif args.command == 'translate':
                    from .translation import translate
                    from .model import Model
                    from .storage import atomic_json
                    from .publish import markdown
                    client=Model(settings);count=0
                    for entry in store.editions():
                        key=entry['day'];doc=store.document(key)
                        changed=translate(doc,client)
                        if changed:
                            atomic_json(settings.data/'editions'/key/'report.json',doc)
                            (settings.data/'editions'/key/'report.md').write_text(markdown(doc,settings))
                            count+=changed
                    result={'translated_articles':count,**build(settings,store)}
                elif args.command == "import-legacy":
                    result = import_legacy(settings, store, args.directory)
                else:
                    result = {"published": len(store.editions()), "sections": len(settings.sections),
                              "notifications": [dict(r) for r in store.db.execute("SELECT channel,state,count(*) AS count FROM deliveries GROUP BY channel,state")],
                              "task_notifications": [dict(r) for r in store.db.execute("SELECT batch_id,channel,state,attempts,last_error FROM notification_outbox")],
                              "account_notifications": [dict(r) for r in store.db.execute("SELECT batch_id,account_id,channel,state,attempts,progress,last_error FROM account_outbox")],
                              "tasks": [dict(r) for r in store.db.execute("SELECT id,period,window_end,state FROM notification_batches")]}
            finally:
                store.close()
        print(json.dumps(result, ensure_ascii=False))
        return 1 if result.get("state") == "partial_failure" else 0
    except BlockingIOError:
        print('{"state":"busy"}')
        return 75
    except Exception as exc:
        print(json.dumps({"state": "failed", "error_type": type(exc).__name__}))
        return 1


if __name__ == "__main__":
    sys.exit(main())
