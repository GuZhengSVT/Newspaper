from __future__ import annotations

import os
import re
import shlex
import tomllib
from dataclasses import dataclass, field, replace
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
TZ = ZoneInfo("Asia/Shanghai")
CONFIG_VERSION = 1
PROMPT_STAGES = {"collect", "compress", "coordinate", "edit", "editorial", "illustrate",
                 "plan", "repair", "review", "select", "translate"}
SEARCH_ENGINES = {"ddg", "bing", "exa", "tavily", "firecrawl", "parallel", "keenable", "anysearch"}
BOUNDED_ENGINES = {"ddg", "bing", "exa", "keenable", "anysearch"}
BOUNDED_ENGINE_DEFAULTS = ["bing", "keenable", "exa"]
HARNESS_ENGINE_DEFAULTS = ["ddg", "bing", "exa"]
SEARCH_LIMITS = {
    "max_queries_per_edition": (40, 1, 40),
    "max_candidates_per_edition": (12, 1, 100),
    "max_fetches_per_edition": (24, 1, 100),
    "max_sources_per_section": (8, 1, 100),
    "queries_per_section": (3, 1, 8),
    "timeout_seconds": (35, 1, 600),
    "collection_timeout_seconds": (300, 1, 3600),
}
MODEL_LIMITS = {"timeout_seconds": (180, 1, 3600), "max_tokens": (12000, 1, 1000000)}
DISCOVERY_LIMITS = {
    "max_indexes_per_edition": (32, 1, 128), "max_links_per_index": (15, 1, 30),
    "max_gap_queries": (3, 0, 40), "max_previews_per_section": (24, 1, 60),
}


class ConfigError(ValueError):
    """Validation error whose message is safe to show without configuration values."""


def release_info() -> dict:
    manifest = ROOT / "release.toml"
    if manifest.is_file():
        return tomllib.loads(manifest.read_text(encoding="utf-8"))
    pyproject = ROOT / "pyproject.toml"
    if pyproject.is_file():
        project = tomllib.loads(pyproject.read_text(encoding="utf-8"))
        return {"version": project["project"]["version"], "commit": None}
    try:
        return {"version": version("newspaper-daily"), "commit": None}
    except PackageNotFoundError:
        return {"version": "unknown", "commit": None}


def read_config(path: Path, description: str) -> dict:
    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except OSError:
        raise ConfigError(f"Cannot read {description}") from None
    except UnicodeError:
        raise ConfigError(f"{description} must be UTF-8") from None
    except tomllib.TOMLDecodeError as exc:
        location = re.search(r" \(at (?:line \d+, column \d+|end of document)\)$", str(exc))
        raise ConfigError(f"Invalid TOML in {description}{location[0] if location else ''}") from None


def check_url(value, name: str) -> str:
    try:
        if not isinstance(value, str) or re.search(r"[\s\\]", value):
            raise ValueError
        url = urlsplit(value)
        if (url.scheme not in {"http", "https"} or not url.hostname or url.username is not None
                or url.query or url.fragment or url.port == 0):
            raise ValueError
    except ValueError:
        raise ConfigError(f"{name} must be an HTTP(S) URL without credentials, query or fragment") from None
    return value.rstrip("/")


def validate_limits(table: str, values: dict, limits: dict) -> None:
    for name, (default, minimum, maximum) in limits.items():
        value = values.get(name, default)
        if type(value) is not int or not minimum <= value <= maximum:
            raise ConfigError(f"{table}.{name} must be an integer from {minimum} to {maximum}")


def validate_choice(table: str, values: dict, name: str, choices: tuple[str, ...]) -> None:
    if values.get(name, choices[0]) not in choices:
        raise ConfigError(f"{table}.{name} must be one of {', '.join(choices)}")


def load_credentials(path: Path) -> None:
    """Read literal KEY=value pairs; never execute shell expressions."""
    if not path.is_file():
        return
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError):
        raise ConfigError("Cannot read the credentials file as UTF-8") from None
    for lineno, line in enumerate(lines, 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, sep, value = line.partition("=")
        if not sep or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            raise ConfigError(f"Invalid credentials file entry on line {lineno}")
        try:
            parsed = shlex.split(value, comments=True)
        except ValueError:
            raise ConfigError(f"Invalid quoting in the credentials file on line {lineno}") from None
        os.environ.setdefault(key, " ".join(parsed))


@dataclass(frozen=True)
class Settings:
    values: dict
    data: Path
    web: Path
    base_url: str
    _prompts: dict = field(default_factory=dict, repr=False, compare=False)
    _bundled_prompts: dict = field(default_factory=dict, repr=False, compare=False)

    @classmethod
    def load(cls, path: str | None = None) -> "Settings":
        config_path = Path(path or os.environ.get("NEWSPAPER_CONFIG", "/etc/newspaper/config.toml"))
        values = read_config(config_path, "runtime configuration")
        allowed = {"config_version", "project_file", "accounts_file", "data_dir", "web_dir", "base_url",
                   "retention_days", "publication", "translation", "model", "search", "notifications",
                   "project", "editions", "sections", "schedule", "homepage", "prompts", "discovery",
                   "publication_timezones"}
        if values.keys() - allowed:
            raise ConfigError("Unknown setting at the root of the runtime configuration")
        if type(values.get("config_version", CONFIG_VERSION)) is not int or values.get(
            "config_version", CONFIG_VERSION
        ) != CONFIG_VERSION:
            raise ConfigError(f"config_version must be {CONFIG_VERSION}")
        for key in ("project_file", "accounts_file", "data_dir", "web_dir"):
            if key in values and (not isinstance(values[key], str) or not values[key].strip()):
                raise ConfigError(f"{key} must be a nonempty path")
        values["_project_dir"] = str(config_path.resolve().parent)
        project_path = values.get('project_file')
        if project_path:
            project_path = (config_path.parent / project_path).resolve()
            project = read_config(project_path, "project configuration")
            allowed = {'project','groups','sections','schedule','homepage','prompts','discovery','publication_timezones'}
            if project.keys() - allowed:
                raise ConfigError("Unknown setting at the root of the project configuration")
            values.update({k:v for k,v in project.items() if k != 'groups'})
            values['editions'] = project.get('groups', [])
            values['_project_dir'] = str(project_path.parent)
        load_credentials(Path(os.environ.get("NEWSPAPER_CREDENTIALS", "/etc/newspaper/credentials.env")))
        if values.get('accounts_file'):
            account_path = (config_path.parent / values['accounts_file']).resolve()
            account_config = read_config(account_path, "accounts configuration")
            if account_config.keys() - {"accounts"}:
                raise ConfigError("Unknown setting in accounts configuration")
            values['_accounts'] = account_config.get('accounts', [])
        data = Path(os.environ.get("NEWSPAPER_DATA", values.get("data_dir", "/var/lib/newspaper"))).resolve()
        web = Path(os.environ.get("NEWSPAPER_WEB", values.get("web_dir", "/srv/newspaper"))).resolve()
        if data == web or data in web.parents or web in data.parents:
            raise ConfigError("News data and published website must be separate directories")
        url = check_url(values.get("base_url", "http://localhost"), "base_url")
        if values.get("retention_days", 30) != 30:
            raise ConfigError("retention_days must be 30")
        result = cls(values, data, web, url)
        result.validate()
        model = values.get("model", {})
        if result.sections:
            required = {"id", "base_url"}
            if values.get("search", {}).get("mode") != "bounded":
                required.add("provider")
            if missing := required - model.keys():
                raise ConfigError(f"Missing model fields: {', '.join(sorted(missing))}")
        result._prompts.update(result.read_prompts())
        for stage in PROMPT_STAGES:
            try:
                result._bundled_prompts[stage] = result.bundled_prompt(stage)
            except (OSError, UnicodeError):
                raise ConfigError(f"Cannot read bundled {stage} prompt as UTF-8") from None
        return result

    @property
    def timezone(self):
        return ZoneInfo(self.values.get('project',{}).get('timezone',TZ.key))

    @property
    def sections(self) -> list[dict]:
        disabled={sid for g in self.values.get('editions',[]) if not g.get('enabled',True) for sid in g['sections']}
        sections = [s for s in self.values.get("sections", []) if s.get("enabled", True) and s['id'] not in disabled]
        ids = [s["id"] for s in sections]
        if len(ids) != len(set(ids)):
            raise ValueError("Duplicate section id")
        return sections

    @property
    def editions(self) -> list[dict]:
        raw = self.values.get('editions', [])
        known={s['id'] for s in self.values.get('sections',[])}
        if any(set(g['sections'])-known for g in raw):raise ValueError('Unknown section in group')
        enabled={s['id'] for s in self.sections}
        configured = [{**g,'sections':[sid for sid in g['sections'] if sid in enabled]} for g in raw if g.get('enabled',True)]
        if raw and not configured and not self.sections:return []
        if not configured:
            return [{"id": "daily" if self.values.get("search", {}).get("mode") == "bounded" else "", "name": "日报", "sections": [s["id"] for s in self.sections]}]
        ids = [e["id"] for e in configured]
        if len(ids) != len(set(ids)) or any(not re.fullmatch(r"[a-z][a-z0-9-]{0,40}", i) for i in ids):
            raise ValueError("Invalid or duplicate edition id")
        section_ids = {s["id"] for s in self.sections}
        assigned = [i for e in configured for i in e["sections"]]
        if len(set(assigned)) != len(assigned) or set(assigned) != section_ids:
            raise ValueError("Each enabled section must belong to exactly one edition")
        return configured

    def for_edition(self, edition: dict) -> "Settings":
        ids = set(edition["sections"])
        return replace(self, values={**self.values, "sections": [s for s in self.sections if s["id"] in ids]})

    def for_preview(self, output: Path) -> "Settings":
        if output.is_symlink():
            raise ConfigError("Preview output must be a new directory")
        output = output.resolve()
        for production in (self.data, self.web):
            if output == production or production in output.parents or output in production.parents:
                raise ConfigError("Preview output must be separate from production data and website directories")
        try:
            output.mkdir(mode=0o700)
        except FileExistsError:
            raise ConfigError("Preview output must be a new directory") from None
        except FileNotFoundError:
            raise ConfigError("Preview output parent directory must exist") from None
        return replace(self, values={**self.values, "publication": {"mode": "local"}, "_accounts": [],
                                     "notifications": {"mode": "legacy", "email": {"enabled": False},
                                                       "telegram": {"enabled": False}}},
                       data=output / "data", web=output / "web", base_url="http://localhost:8000")

    def effective(self) -> dict:
        model = self.values.get("model", {})
        search = self.values.get("search", {})
        discovery = self.values.get("discovery", {})
        translation = self.values.get("translation", {})
        notifications = self.values.get("notifications", {})
        publication = self.values.get("publication", {})
        return {
            "release": release_info(),
            "config_version": CONFIG_VERSION,
            "timezone": self.timezone.key,
            "groups": len(self.editions),
            "sections": len(self.sections),
            "publication": {"mode": publication.get("mode", "local"),
                            "pages_target_configured": bool(publication.get("repository")),
                            "pages_credentials_configured": bool(os.environ.get("NEWSPAPER_PAGES_TOKEN"))},
            "model": {"coordination": model.get("coordination", "model"),
                      "manager_reasoning_effort": model.get("manager_reasoning_effort", "max"),
                      "worker_reasoning_effort": model.get("worker_reasoning_effort", "low"),
                      "credentials_configured": bool(os.environ.get(model.get("api_key_env", "NEWSPAPER_API_KEY"))),
                      "max_tokens_per_edition": model.get("max_tokens_per_edition"),
                      **{key: model.get(key, spec[0]) for key, spec in MODEL_LIMITS.items()}},
            "search": {"mode": search.get("mode", "harness"),
                       "engines": search.get("engines", BOUNDED_ENGINE_DEFAULTS if search.get("mode") == "bounded"
                                             else HARNESS_ENGINE_DEFAULTS),
                       **{key: search.get(key, spec[0]) for key, spec in SEARCH_LIMITS.items()}},
            "discovery": {key: discovery.get(key, spec[0]) for key, spec in DISCOVERY_LIMITS.items()},
            "translation": {"enabled": translation.get("enabled", False),
                            "publish": translation.get("publish", "together")},
            "notifications": {"mode": notifications.get("mode", "legacy"),
                              "email_enabled": notifications.get("email", {}).get("enabled", False),
                              "telegram_enabled": notifications.get("telegram", {}).get("enabled", False),
                              "failure_alerts_enabled": "failure_alerts" in notifications},
        }

    def validate(self) -> None:
        from .schedule import validate_schedule
        from .accounts import accounts, account_mode
        for name in ("project", "schedule", "homepage", "prompts", "discovery", "translation", "model",
                     "search", "notifications", "publication", "publication_timezones"):
            if not isinstance(self.values.get(name, {}), dict):
                raise ConfigError(f"{name} must be a table")
        for name in ("sections", "editions"):
            items = self.values.get(name, [])
            if not isinstance(items, list) or any(
                not isinstance(item, dict) or not isinstance(item.get("id"), str)
                or type(item.get("enabled", True)) is not bool
                or type(item.get("notify", True)) is not bool
                for item in items
            ):
                raise ConfigError(f"{name} must be tables with string ids and boolean switches")
        if any(not isinstance(item.get("sections"), list) or any(not isinstance(sid, str) for sid in item["sections"])
               for item in self.values.get("editions", [])):
            raise ConfigError("Each group must list its section ids")
        for channel in ("email", "telegram"):
            cfg = self.values.get("notifications", {}).get(channel, {})
            if not isinstance(cfg, dict) or type(cfg.get("enabled", False)) is not bool:
                raise ConfigError(f"notifications.{channel}.enabled must be boolean")
        if self.values.get('notifications',{}).get('mode','legacy') not in {'legacy','accounts'}:
            raise ValueError('Invalid notification mode')
        accounts(self)
        alerts = self.values.get('notifications', {}).get('failure_alerts')
        if alerts is not None:
            if (not isinstance(alerts, dict) or set(alerts) != {'account_id'}
                or not isinstance(alerts['account_id'], str)
                or alerts['account_id'] not in {a['id'] for a in self.values.get('_accounts', [])}):
                raise ValueError('Failure alerts require one known account_id')
        if account_mode(self) and not (self.values.get('project') or 'schedule' in self.values):
            raise ValueError('Account notifications require a project or schedule')
        if type(self.values.get('translation',{}).get('enabled',False)) is not bool:
            raise ValueError('Translation enabled must be boolean')
        if any(value for a in accounts(self) for value in a['options'].values()) and not self.values.get('translation',{}).get('enabled',False):
            raise ValueError('Bilingual subscriptions require translation.enabled')
        try:
            self.timezone
        except (KeyError, ValueError, TypeError):
            raise ConfigError("project.timezone must be a valid IANA time zone") from None
        self.editions  # Validate group/section assignment before any work begins.
        model = self.values.get('model', {})
        if model.keys() - (MODEL_LIMITS.keys() | {"coordination", "provider", "id", "base_url", "api_key_env",
                                                 "manager_reasoning_effort", "worker_reasoning_effort",
                                                 "retry_delays_seconds", "max_tokens_per_edition"}):
            raise ConfigError("Unknown model setting")
        validate_choice("model", model, "coordination", ("model", "configured"))
        validate_limits("model", model, MODEL_LIMITS)
        if "max_tokens_per_edition" in model and (
            type(model["max_tokens_per_edition"]) is not int or model["max_tokens_per_edition"] < 1
        ):
            raise ConfigError("model.max_tokens_per_edition must be a positive integer")
        if "base_url" in model:
            check_url(model["base_url"], "model.base_url")
        for name in ("provider", "id"):
            if name in model and (not isinstance(model[name], str) or not model[name].strip()):
                raise ConfigError(f"model.{name} must be a nonempty string")
        key_env = model.get("api_key_env", "NEWSPAPER_API_KEY")
        if not isinstance(key_env, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key_env):
            raise ConfigError("model.api_key_env must be an environment variable name")
        delays = model.get("retry_delays_seconds", [15, 60])
        if not isinstance(delays, list) or len(delays) != 2 or any(
            type(value) is not int or not 0 <= value <= 600 for value in delays
        ):
            raise ConfigError("model.retry_delays_seconds must contain two integers from 0 to 600")
        if model.get('manager_reasoning_effort', 'max') != 'max' or model.get('worker_reasoning_effort', 'low') != 'low':
            raise ValueError('Board coordinators require max reasoning and task workers require low reasoning')
        search = self.values.get("search", {})
        if search.keys() - (SEARCH_LIMITS.keys() | {"mode", "engines"}):
            raise ConfigError("Unknown search setting")
        validate_choice("search", search, "mode", ("harness", "bounded"))
        validate_limits("search", search, SEARCH_LIMITS)
        bounded = search.get("mode") == "bounded"
        supported = BOUNDED_ENGINES if bounded else SEARCH_ENGINES
        engines = search.get("engines", BOUNDED_ENGINE_DEFAULTS if bounded else HARNESS_ENGINE_DEFAULTS)
        if not isinstance(engines, list) or not engines or any(
            not isinstance(engine, str) or engine not in supported for engine in engines
        ) or len(set(engines)) != len(engines):
            raise ConfigError(f"search.engines must list distinct engines from {', '.join(sorted(supported))}")
        if bounded and len(engines) > 3:
            raise ConfigError("Bounded search supports at most three engines")
        discovery = self.values.get("discovery", {})
        if discovery.keys() - DISCOVERY_LIMITS.keys():
            raise ConfigError("Unknown discovery setting")
        validate_limits("discovery", discovery, DISCOVERY_LIMITS)
        translation = self.values.get("translation", {})
        if translation.keys() - {"enabled", "publish"}:
            raise ConfigError("Unknown translation setting")
        validate_choice("translation", translation, "publish", ("together", "chinese_first"))
        publication = self.values.get("publication", {})
        if publication.keys() - {"mode", "repository", "ref"}:
            raise ConfigError("Unknown publication setting")
        validate_choice("publication", publication, "mode", ("local", "pages", "pages_branch"))
        if publication.get("mode") in {"pages", "pages_branch"}:
            repository = publication.get("repository")
            if not isinstance(repository, str) or not re.fullmatch(r"[A-Za-z0-9-]+/[A-Za-z0-9_.-]+", repository):
                raise ConfigError("publication.repository must be a GitHub owner/repository pair")
            ref = publication.get("ref", "main")
            if (not isinstance(ref, str) or not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_./-]*", ref)
                    or ".." in ref or any(part.startswith(".") or part.endswith((".", ".lock")) or not part
                                          for part in ref.split("/"))):
                raise ConfigError("publication.ref must be a GitHub branch or tag")
            origin = urlsplit(self.base_url)
            if origin.scheme != "https" or origin.path:
                raise ConfigError("Pages publication requires an HTTPS website root as base_url")
        validate_schedule(self.values.get('schedule', {}))
        for group in self.editions:validate_schedule({**self.values.get('schedule',{}), **group.get('schedule',{})})
        for section in self.sections:
            if not re.fullmatch(r'[a-z][a-z0-9-]{0,40}', section['id']):raise ValueError('Invalid section id')
            parent=next((g for g in self.editions if section['id'] in g['sections']),{})
            validate_schedule({**self.values.get('schedule',{}), **parent.get('schedule',{}), **section.get('schedule',{})})
            fmt=section.get('format',{})
            if set(fmt)-{'max_characters','max_major_characters','max_paragraphs','max_major_paragraphs'}:raise ValueError('Unknown section format')
            if any(type(v) is not int or not 1<=v<=(12 if 'paragraphs' in k else 10000) for k,v in fmt.items()):raise ValueError('Invalid section format bounds')
            search=section.get('search',{})
            if type(search.get('query_count',2)) is not int or not 1<=search.get('query_count',2)<=8:raise ValueError('Invalid query count')
            if not isinstance(search.get('queries',[]),list) or any(not isinstance(q,str) or not q.strip() or len(q)>220 for q in search.get('queries',[])):raise ValueError('Invalid fixed query')
            if not isinstance(search.get('domains',[]),list) or any(not isinstance(d,str) or not re.fullmatch(r'[a-zA-Z0-9.-]+',d) for d in search.get('domains',[])):raise ValueError('Invalid search domain')
            rules=section.get('filter',{})
            if set(rules)-{'importance','reliability','exclude_keywords','require_primary'}:raise ValueError('Unknown section filter')
            if not set(rules.get('importance',['重大','重要'])) <= {'重大','重要'}:raise ValueError('Invalid importance filter')
            if not set(rules.get('reliability',['A','B'])) <= {'A','B'}:raise ValueError('Invalid reliability filter')
            for source in section.get('sources',[]):
                from .research import source_url
                source_url(source['url'])
                if source.get('kind','index') not in {'index','rss','page'}:raise ValueError('Invalid source kind')
                if type(source.get('enabled',True)) is not bool:raise ValueError('Source enabled must be boolean')
                if type(source.get("required", False)) is not bool:
                    raise ConfigError("Section source required must be boolean")
                if source.get('link_pattern'):
                    if not isinstance(source['link_pattern'],str) or len(source['link_pattern'])>500:raise ValueError('Invalid source link pattern')
                    re.compile(source['link_pattern'])
            instructions = section.get("prompts", {})
            if (not isinstance(instructions, dict)
                    or instructions.keys() - {"plan", "select", "edit", "review", "repair", "compress", "collect"}
                    or any(not isinstance(value, str) for value in instructions.values())):
                raise ConfigError("Invalid section prompt")
        home=self.values.get('homepage',{})
        if home.get('mode','groups') not in {'groups','sections','index'}:raise ValueError('Invalid homepage mode')
        if home.get('layout','tabs') not in {'tabs','columns'}:raise ValueError('Invalid homepage layout')
        if type(home.get('layout_switch',True)) is not bool:raise ValueError('Homepage layout_switch must be boolean')
        if not set(home.get('groups',[e['id'] for e in self.editions])) <= {e['id'] for e in self.values.get('editions',self.editions)}:raise ValueError('Unknown homepage group')
        if not set(home.get('sections',[])) <= {s['id'] for s in self.values.get('sections',[])}:raise ValueError('Unknown homepage section')

    def read_prompts(self) -> dict:
        cfg = self.values.get("prompts", {})
        if cfg.keys() - (PROMPT_STAGES | {stage + "_file" for stage in PROMPT_STAGES}):
            raise ConfigError("Unknown project prompt stage")
        if any(not isinstance(value, str) for value in cfg.values()):
            raise ConfigError("Project prompts and prompt paths must be strings")
        result = {}
        for stage in PROMPT_STAGES:
            text = cfg.get(stage, "")
            file = cfg.get(stage + "_file")
            if file:
                try:
                    text += "\n" + (Path(self.values.get("_project_dir", ".")) / file).read_text(encoding="utf-8")
                except (OSError, UnicodeError, ValueError):
                    raise ConfigError(f"Cannot read prompts.{stage}_file as UTF-8") from None
            result[stage] = text
        return result

    def prompt_text(self, stage: str, sections: list[dict] | None = None) -> str:
        text = (self._prompts or self.read_prompts()).get(stage, "")
        for section in sections if sections is not None else self.sections:
            instruction=section.get('prompts',{}).get(stage,'')
            if instruction:text += '\n仅适用于板块 '+section['id']+'（'+section['name']+'）：'+instruction
        return text

    def bundled_prompt(self, stage: str) -> str:
        if stage in self._bundled_prompts:
            return self._bundled_prompts[stage]
        return (ROOT / "prompts" / f"{stage}.md").read_text(encoding="utf-8")

    def prepare(self) -> None:
        for path in [self.data, self.data / "editions", self.data / "tmp", self.web, self.web / "releases"]:
            path.mkdir(parents=True, exist_ok=True)
