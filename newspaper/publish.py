from __future__ import annotations

import copy
import json
import os
import re
import shutil
import uuid
from datetime import datetime
from email.utils import format_datetime
from pathlib import Path
from xml.etree.ElementTree import Element, SubElement, tostring

from jinja2 import Environment, FileSystemLoader, select_autoescape
from markdown_it import MarkdownIt
from markupsafe import Markup

from .config import ROOT, TZ, Settings
from .storage import Store, utcnow
from .reading import report_view

MARKDOWN = MarkdownIt("commonmark", {"html": False})
LABELS = {"official_statement": "官方发布", "single_source": "单一来源", "corroborated": "多源核实"}


def markdown(doc: dict) -> str:
    view = report_view(doc, doc["date"])
    text = ["# " + doc["title"], ""]
    if view["window"]:
        text += ["报道时段：" + view["window"], ""]
    if not view["count"]:
        text += ["本期未收录新闻。", ""]
    for section in view["sections"]:
        text += ["## " + section["name"], ""]
        for article in section["articles"]:
            text += ["### " + article["title"], ""]
            metadata = [article["time"]]
            if article.get("importance"):
                metadata += [article["importance"], "可靠性 " + article["reliability"]]
            if article["follow_up"]:
                metadata.append("进展")
            text += [" · ".join(metadata), ""]
            for paragraph in article["paragraphs"]:
                text += [paragraph, ""]
            if article["references"]:
                links = " · ".join(f'[{ref["label"]}](<{ref["url"]}>)' for ref in article["references"])
                text += ["来源：" + links, ""]
    return "\n".join(text)


def edition_options(settings: Settings) -> list[dict]:
    editions = (settings.editions if settings.values.get("editions") else None) or [
        {"id": "technology-politics", "name": "科技与政治日报"}, {"id": "acgn", "name": "ACGN 日报"}]
    return [{**e, "label": {"technology-politics": "科技｜政治", "acgn": "ACGN"}.get(e["id"], e["name"])} for e in editions]


def build(settings: Settings, store: Store, now: float | None = None) -> dict:
    now = utcnow().timestamp() if now is None else now
    entries = store.editions(now)
    editions = edition_options(settings)
    documents = {entry["day"]: store.document(entry["day"]) for entry in entries}
    today = datetime.fromtimestamp(now, TZ).date().isoformat()
    columns = []
    for edition in editions:
        key = today + "/" + edition["id"]
        view = report_view(documents[key], key) if key in documents else None
        columns.append({**edition, "anchor": "column-" + edition["id"], "view": view})
    toc_groups = [{"label": c["label"], "anchor": c["anchor"],
                   "sections": c["view"]["sections"] if c["view"] else []} for c in columns]
    release = settings.web / "releases" / uuid.uuid4().hex
    release.mkdir(parents=True)
    env = Environment(loader=FileSystemLoader(ROOT / "templates"), autoescape=select_autoescape(["html", "xml"]))
    env.globals["labels"] = LABELS
    try:
        shutil.copytree(ROOT / "static", release / "assets")
        search_index = []
        for entry in entries:
            day = entry["day"]
            doc = documents[day]
            directory = release / "daily" / day
            directory.mkdir(parents=True, exist_ok=True)
            source = settings.data / "editions" / day
            md = (source / "report.md").read_text() if entry["legacy"] else markdown(doc)
            legacy_body = md
            if legacy_body.startswith("---\n") and "\n---" in legacy_body[4:]:
                legacy_body = legacy_body.split("\n---", 1)[1].lstrip()
            legacy_body = re.sub(r"<!--\s*news-ledger\s*\n.*?-->", "", legacy_body, flags=re.S)
            if legacy_body.startswith("# "):
                legacy_body = legacy_body.split("\n", 1)[-1].lstrip()
            legacy_html = Markup(MARKDOWN.render(legacy_body)) if entry["legacy"] else None
            view = report_view(doc, day)
            daily_toc = [{"label": "本期新闻", "anchor": "daily-report", "sections": view["sections"]}]
            if legacy_html:
                # Add stable anchors to original Markdown headings without changing downloads.
                tokens = MARKDOWN.parse(legacy_body)
                headings = []
                for index, token in enumerate(tokens):
                    if token.type == "heading_open" and index + 1 < len(tokens):
                        anchor = "legacy-heading-" + str(len(headings) + 1)
                        token.attrSet("id", anchor)
                        token.attrSet("data-outline", "")
                        headings.append({"anchor": anchor, "title": tokens[index + 1].content})
                legacy_html = Markup(MARKDOWN.renderer.render(tokens, MARKDOWN.options, {}))
                daily_toc[0]["sections"] = [{"anchor": "daily-report", "name": "正文", "articles": headings}]
            (directory / "index.html").write_text(env.get_template("daily.html").render(
                doc=doc, document_key=day, legacy_html=legacy_html, view=view, toc_groups=daily_toc,
            ))
            downloads = release / "downloads"
            downloads.mkdir(exist_ok=True)
            (downloads / f"{day}.md").parent.mkdir(parents=True, exist_ok=True)
            (downloads / f"{day}.md").write_text(md)
            # Only published report data is exposed, never raw evidence or credentials.
            (downloads / f"{day}.json").write_text(json.dumps(doc, ensure_ascii=False, indent=2))
            search_index.append({"date": day, "title": entry["title"], "summary": entry["summary"],
                                 "text": " ".join([entry["summary"]] + [a["title"] + " " + a["summary"] + " " +
                                     " ".join(f["text"] for f in a["facts"])
                                     for s in doc.get("sections", []) for a in s["articles"]])})
        (release / "index.html").write_text(env.get_template("index.html").render(
            today=today, today_label=datetime.fromtimestamp(now, TZ).strftime("%Y年%m月%d日"),
            columns=columns, toc_groups=toc_groups,
        ))
        has_legacy = any(e["legacy"] for e in entries)
        archives = [("", "往期日报", entries)]
        for edition in editions:
            archives.append((edition["id"], edition["label"] + " · 往期日报",
                             [e for e in entries if e["day"].endswith("/" + edition["id"])]))
        if has_legacy:
            archives.append(("legacy", "历史存档", [e for e in entries if e["legacy"]]))
        for archive_id, title, archive_entries in archives:
            directory = release / "archive" / archive_id
            directory.mkdir(parents=True, exist_ok=True)
            (directory / "index.html").write_text(env.get_template("archive.html").render(
                entries=archive_entries, archive_title=title, archive_id=archive_id,
                editions=editions, has_legacy=has_legacy))
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
