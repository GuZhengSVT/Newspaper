from __future__ import annotations

import copy
import json
import os
import shutil
import uuid
from datetime import datetime
from email.utils import format_datetime
from pathlib import Path
from xml.etree.ElementTree import Element, SubElement, tostring

from jinja2 import Environment, FileSystemLoader, select_autoescape
from markdown_it import MarkdownIt
from markupsafe import Markup

from .config import ROOT, Settings
from .storage import Store, utcnow

MARKDOWN = MarkdownIt("commonmark", {"html": False})
LABELS = {"official_statement": "官方发布", "single_source": "单一来源", "corroborated": "多源核实"}


def markdown(doc: dict) -> str:
    text = ["---", f'title: {json.dumps(doc["title"], ensure_ascii=False)}', f'date: "{doc["date"]}"',
            f'generated_at: "{doc["generated_at"]}"', 'model: "d1api/deepseek-v4.1-flash"',
            f'schema_version: {doc["schema_version"]}', "---", "", "# " + doc["title"], "", doc["summary"], "",
            f'报道窗口：{doc["window_start"]} 至 {doc["window_end"]}', "", "## 今日重点", ""]
    text.extend("- " + item for item in doc["highlights"])
    sources = {s["id"]: s for s in doc["sources"]}
    for section in doc["sections"]:
        if not section["articles"]:
            continue
        text += ["", "## " + section["name"], ""]
        for article in section["articles"]:
            text += ["### " + article["title"] + ("【跟进】" if article["follow_up"] else ""), "", article["summary"], "",
                     "报道时间：" + article["published_at"], "核实状态：" + LABELS[article["verification"]], ""]
            if article.get("importance"):
                text += [f'重要性：{article["importance"]} · 可靠性：{article["reliability"]}', ""]
            if article["new_facts"]:
                text += ["新增进展：" + article["new_facts"], ""]
            for fact in article["facts"]:
                cited = dict.fromkeys(e["source_id"] for e in fact["evidence"])
                links = " ".join(f'[{sources[sid]["title"]}](<{sources[sid]["url"]}>)' for sid in cited)
                text += [fact["text"], "", "来源：" + links, ""]
            if article.get("limitations"):
                text += ["不确定性与限制：" + article["limitations"], ""]
    if doc.get("coverage_notes"):
        text += ["", "## 覆盖说明", ""] + ["- " + n for n in doc["coverage_notes"]]
    text += ["", "## 来源清单", ""]
    text.extend(f'- [{s["title"]}](<{s["url"]}>)' for s in doc["sources"])
    return "\n".join(text) + "\n"


def build(settings: Settings, store: Store, now: float | None = None) -> dict:
    now = utcnow().timestamp() if now is None else now
    entries = store.editions(now)
    release = settings.web / "releases" / uuid.uuid4().hex
    release.mkdir(parents=True)
    env = Environment(loader=FileSystemLoader(ROOT / "templates"), autoescape=select_autoescape(["html", "xml"]))
    env.globals["labels"] = LABELS
    try:
        shutil.copytree(ROOT / "static", release / "assets")
        search_index = []
        for entry in entries:
            day = entry["day"]
            doc = store.document(day)
            directory = release / "daily" / day
            directory.mkdir(parents=True, exist_ok=True)
            source = settings.data / "editions" / day
            md = (source / "report.md").read_text()
            legacy_body = md
            if legacy_body.startswith("---\n") and "\n---" in legacy_body[4:]:
                legacy_body = legacy_body.split("\n---", 1)[1].lstrip()
            legacy_html = Markup(MARKDOWN.render(legacy_body)) if entry["legacy"] else None
            (directory / "index.html").write_text(env.get_template("daily.html").render(
                doc=doc, document_key=day, legacy_html=legacy_html, entries=entries, sources={s["id"]: s for s in doc.get("sources", [])},
            ))
            downloads = release / "downloads"
            downloads.mkdir(exist_ok=True)
            (downloads / f"{day}.md").parent.mkdir(parents=True, exist_ok=True)
            (downloads / f"{day}.md").write_text(md)
            # Only published report data is exposed, never raw evidence or credentials.
            (downloads / f"{day}.json").write_text(json.dumps(doc, ensure_ascii=False, indent=2))
            search_index.append({"date": day, "title": entry["title"], "summary": entry["summary"],
                                 "text": " ".join([entry["summary"]] + [a["title"] + " " + a["summary"] for s in doc.get("sections", []) for a in s["articles"]])})
        (release / "index.html").write_text(env.get_template("index.html").render(
            entries=entries, sections=settings.sections, editions=settings.values.get("editions", []), built_at=datetime.fromtimestamp(now).astimezone().isoformat(),
        ))
        (release / "search.json").write_text(json.dumps(search_index, ensure_ascii=False))
        rss = Element("rss", version="2.0")
        channel = SubElement(rss, "channel")
        for key, value in {"title": "Newspaper", "link": settings.base_url, "description": "每日新闻 · 仅保留最近 30 天"}.items():
            SubElement(channel, key).text = value
        for entry in entries:
            item = SubElement(channel, "item")
            link = settings.base_url + "/daily/" + entry["day"] + "/"
            for key, value in {"title": entry["title"], "link": link, "guid": link, "description": entry["summary"]}.items():
                SubElement(item, key).text = value
            doc = store.document(entry["day"])
            SubElement(item, "pubDate").text = format_datetime(datetime.fromisoformat(doc["generated_at"]))
        (release / "feed.xml").write_bytes(tostring(rss, encoding="utf-8", xml_declaration=True))
        for edition in settings.values.get("editions", []):
            feed = copy.deepcopy(rss)
            ch = feed.find("channel")
            ch.find("title").text = edition["name"]
            for item in list(ch.findall("item")):
                if not item.findtext("link", "").rstrip("/").endswith("/" + edition["id"]):
                    ch.remove(item)
            (release / ("feed-" + edition["id"] + ".xml")).write_bytes(tostring(feed, encoding="utf-8", xml_declaration=True))
        (release / "robots.txt").write_text("User-agent: *\nDisallow: /downloads/\n")
        link = settings.web / (".current-" + uuid.uuid4().hex)
        link.symlink_to(release.relative_to(settings.web), target_is_directory=True)
        os.replace(link, settings.web / "current")
    except BaseException:
        shutil.rmtree(release, ignore_errors=True)
        raise
    # Keep one complete release. Old builds must not bypass data retention.
    for old in (settings.web / "releases").iterdir():
        if old != release:
            shutil.rmtree(old)
    return {"editions": len(entries), "release": release.name}
