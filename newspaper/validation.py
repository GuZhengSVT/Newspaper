from __future__ import annotations

import json
from urllib.parse import urlsplit
from datetime import datetime

from jsonschema import Draft202012Validator, FormatChecker

from .config import ROOT, Settings
from .research import source_url

SCHEMA = json.loads((ROOT / "schemas" / "report.schema.json").read_text())
VALIDATOR = Draft202012Validator(SCHEMA, format_checker=FormatChecker())


def normalize(text: str) -> str:
    return " ".join(text.split()).casefold()


def validate_report(doc: dict, settings: Settings, sources: list[dict], history: list[dict], window: dict) -> None:
    VALIDATOR.validate(doc)
    if doc["date"] != window["date"] or any(doc[k] != window[k] for k in ["window_start", "window_end"]):
        raise ValueError("Report date/window differs from the scheduled edition")
    expected = [(s["id"], s["name"]) for s in settings.sections]
    if [(s["id"], s["name"]) for s in doc["sections"]] != expected:
        raise ValueError("Report must preserve the configured sections and their order")
    source_map = {s["id"]: s for s in sources}
    ids = [s["id"] for s in doc["sources"]]
    if len(set(ids)) != len(ids):
        raise ValueError("Duplicate source id")
    for item in doc["sources"]:
        if item["id"] not in source_map or item["url"] != source_map[item["id"]]["url"]:
            raise ValueError("Unknown or modified source URL")
        source_url(item["url"])
    start, end = (datetime.fromisoformat(window[k]) for k in ["window_start", "window_end"])
    historical = {normalize(e["id"]) for e in history}
    seen = set()
    for section in doc["sections"]:
        if not section["articles"] and not section["empty_reason"].strip():
            raise ValueError("An empty section needs an explanation")
        for article in section["articles"]:
            moment = datetime.fromisoformat(article["published_at"])
            if moment.tzinfo is None or not start <= moment < end:
                raise ValueError("Article is outside the edition's reporting window")
            identity = normalize(article["event_key"])
            if identity in seen:
                raise ValueError("Duplicate event in the same edition")
            if identity in historical and not article["follow_up"]:
                raise ValueError("Previously reported event is missing a substantive follow-up")
            if article["follow_up"] and not article["new_facts"].strip():
                raise ValueError("Follow-up needs explicit new facts")
            seen.add(identity)
            if doc.get("schema_version") == 2 and article["reliability"] == "A" and article["verification"] == "single_source":
                raise ValueError("A reliability requires primary authority or independent corroboration")
            referenced = set()
            for fact in article["facts"]:
                if (doc.get("schema_version") == 2 and article["verification"] == "corroborated"
                    and fact.get("kind", "fact") == "fact"):
                    domains = {urlsplit(source_map[e["source_id"]]["url"]).hostname
                               for e in fact["evidence"] if e["source_id"] in source_map}
                    if len(domains) < 2:
                        raise ValueError("Each corroborated core fact needs distinct source origins; independence also needs review")
                for evidence in fact["evidence"]:
                    sid = evidence["source_id"]
                    if sid not in ids or sid not in source_map:
                        raise ValueError("Fact cites an unknown source")
                    if normalize(evidence["quote"]) not in normalize(source_map[sid]["content"]):
                        raise ValueError("Evidence quotation was not found in the fetched source")
                    referenced.add(sid)
            if article["verification"] == "corroborated" and len(referenced) < 2:
                raise ValueError("Corroboration requires at least two source records")
