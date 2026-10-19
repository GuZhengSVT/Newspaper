from __future__ import annotations

import hashlib
import json
import os
import shutil
import signal
import subprocess
from concurrent.futures import ThreadPoolExecutor
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode

from .config import ROOT, Settings
from .model import parse_json
from .storage import utcnow


def source_url(url: str) -> str:
    parts = urlsplit(url)
    if parts.scheme != "https" or not parts.hostname or parts.username or parts.password:
        raise ValueError("Source must have a public HTTPS URL without credentials")
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
             if not k.casefold().startswith("utm_") and k.casefold() not in {"fbclid", "gclid", "msclkid"}]
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path or "/", urlencode(query), ""))


class TextParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.skip = 0
        self.text: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style", "noscript"}:
            self.skip += 1

    def handle_endtag(self, tag):
        if tag in {"script", "style", "noscript"} and self.skip:
            self.skip -= 1

    def handle_data(self, data):
        if not self.skip:
            self.text.append(data)


def page_text(text: str, kind: str) -> str:
    if kind == "html":
        parser = TextParser()
        parser.feed(text)
        text = " ".join(parser.text)
    return " ".join(text.split())[:20000]


def prepare_profile(work: Path, settings: Settings) -> tuple[Path, dict]:
    home = work / "dsh"
    (work / "home").mkdir()
    profile = home / "profiles" / "newspaper"
    shutil.copytree(ROOT / "dsh" / "profile", profile)
    (profile / "node_modules").symlink_to(ROOT / "node_modules", target_is_directory=True)
    # Runtime overlay contains configuration references, never credential values.
    model = settings.values["model"]
    search = settings.values.get("search", {})
    overlay = [
        {"id": "agent-default-model", "config": {"provider": model["provider"], "model": model["id"], "reasoningEffort": "high"}},
        {"id": "llm-pi-ai", "config": {"providers": {model["provider"]: {
            "displayName": model["provider"], "apiKeyEnv": model.get("api_key_env", "D1API_API_KEY"),
            "api": "openai-completions", "baseURL": model["base_url"].rstrip("/") + "/",
            "models": [{"id": model["id"], "name": model["id"], "input": ["text"],
                        "contextWindow": 1000000, "maxTokens": model.get("max_tokens", 12000),
                        "reasoningEfforts": {"low": "low", "high": "high", "max": "max"}}],
        }}}},
        {"id": "web-search-free", "config": {"provider": "multi", "cache": False,
          "disabledEngines": [e for e in ["ddg", "bing", "exa", "tavily", "firecrawl", "parallel", "keenable", "anysearch"] if e not in search.get("engines", ["ddg", "bing", "exa"])],
          "multiMaxEngines": len(search.get("engines", ["ddg", "bing", "exa"]))}},
    ]
    overlay_path = work / "overlay.json"
    overlay_path.write_text(json.dumps(overlay))  # JSON is also valid YAML.
    env = {**os.environ, "HOME": str(work / "home"), "XDG_CACHE_HOME": str(work / "cache"),
           "XDG_CONFIG_HOME": str(work / "home" / ".config"),
           "DSH_HOME": str(home), "RESEARCH_CACHE_DIR": str(work / "cache"),
           "NODE_OPTIONS": "--max-old-space-size=256", "NO_COLOR": "1"}
    return overlay_path, env


def collect(settings: Settings, section: dict, window: dict, work: Path, supplement: str = "") -> dict:
    work.mkdir(parents=True)
    overlay, env = prepare_profile(work, settings)
    prompt = (ROOT / "prompts" / "collect.md").read_text() + "\n\n输入：\n" + json.dumps({
        "section": section, **window, "supplement": supplement,
        "max_sources": settings.values.get("search", {}).get("max_sources_per_section", 8),
        "query_count": section.get("query_count", settings.values.get("search", {}).get("queries_per_section", 3)),
        "sections": section.get("sections", [section]),
    }, ensure_ascii=False)
    command = [str(ROOT / "node_modules" / ".bin" / "dsh"), "--profile", "newspaper", "--patch", str(overlay), "-"]
    with (work / "stdout.txt").open("w") as stdout, (work / "stderr.txt").open("w") as stderr:
        process = subprocess.Popen(command, cwd=work, env=env, stdin=subprocess.PIPE,
                                   stdout=stdout, stderr=stderr, text=True, start_new_session=True)
        try:
            process.communicate(prompt, timeout=settings.values.get("search", {}).get("collection_timeout_seconds", 300))
            if process.returncode:
                raise RuntimeError("DSH collection failed")
        finally:
            # Also terminate subprocesses a tool might have left behind.
            try:
                os.killpg(process.pid, signal.SIGTERM)
                process.wait(timeout=3)
            except ProcessLookupError:
                pass
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
    result = parse_json((work / "stdout.txt").read_text())
    if not isinstance(result.get("sources"), list):
        raise ValueError("DSH did not return a source list")
    return result


def fetch_source(settings: Settings, candidate: dict, work: Path) -> dict | None:
    try:
        url = source_url(candidate["url"])
        env = {**os.environ, "RESEARCH_CACHE_DIR": str(work / "cache"), "NODE_OPTIONS": "--max-old-space-size=128"}
        result = subprocess.run(
            ["node", str(ROOT / "scripts" / "fetch.mjs")],
            input=json.dumps({"url": url}), capture_output=True, text=True, env=env,
            timeout=settings.values.get("search", {}).get("timeout_seconds", 35) + 5, check=True,
        )
        data = parse_json(result.stdout)
        text = page_text(data["content"], data["kind"])
        if len(text) < 100:
            return None
        return {"id": "s-" + hashlib.sha256(url.encode()).hexdigest()[:16], "url": url,
                "title": str(candidate.get("title", ""))[:300],
                "published_at": candidate.get("published_at"), "content": text, "fetched_at": utcnow().isoformat()}
    except (ValueError, KeyError, subprocess.SubprocessError):
        return None


def gather(settings: Settings, section: dict, window: dict, work: Path, supplement: str = "", cache: dict | None = None) -> dict:
    raw = collect(settings, section, window, work, supplement)
    candidates = {}
    for item in raw["sources"]:
        if not isinstance(item, dict):
            continue
        try:
            candidates[source_url(item["url"])] = item
        except (ValueError, KeyError, TypeError):
            continue
    limit = settings.values.get("search", {}).get("max_sources_per_section", 8)
    cache = cache if cache is not None else {}
    selected = list(candidates)[:limit]
    missing = [candidates[url] for url in selected if url not in cache]
    with ThreadPoolExecutor(max_workers=2) as pool:
        fetched = list(pool.map(lambda c: fetch_source(settings, c, work), missing))
    for candidate, result in zip(missing, fetched):
        cache[source_url(candidate["url"])] = result
    sources = [cache[url] for url in selected if cache[url]]
    # Identical syndicated bodies add no independent evidence.
    bodies = {}
    for source in sources:
        bodies.setdefault(hashlib.sha256(source["content"].encode()).hexdigest(), source)
    return {"section_id": section["id"], "sources": list(bodies.values()),
            "notes": raw.get("notes", [])}
