from __future__ import annotations

import os
import shlex
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
TZ = ZoneInfo("Asia/Shanghai")


def load_credentials(path: Path) -> None:
    """Read literal KEY=value pairs; never execute shell expressions."""
    if not path.is_file():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, sep, value = line.partition("=")
        if not sep or not key.replace("_", "").isalnum():
            raise ValueError("Invalid credentials file entry")
        parsed = shlex.split(value, comments=True)
        os.environ.setdefault(key, " ".join(parsed))


@dataclass(frozen=True)
class Settings:
    values: dict
    data: Path
    web: Path
    base_url: str

    @classmethod
    def load(cls, path: str | None = None) -> "Settings":
        config_path = Path(path or os.environ.get("NEWSPAPER_CONFIG", "/etc/newspaper/config.toml"))
        values = tomllib.loads(config_path.read_text())
        project_path = values.get('project_file')
        if project_path:
            project_path = (config_path.parent / project_path).resolve()
            project = tomllib.loads(project_path.read_text())
            allowed = {'project','groups','sections','schedule','homepage','prompts','discovery','publication_timezones'}
            if set(project) - allowed:raise ValueError('Unknown project settings: ' + ', '.join(sorted(set(project)-allowed)))
            values.update({k:v for k,v in project.items() if k != 'groups'})
            values['editions'] = project.get('groups', [])
            values['_project_dir'] = str(project_path.parent)
        load_credentials(Path(os.environ.get("NEWSPAPER_CREDENTIALS", "/etc/newspaper/credentials.env")))
        if values.get('accounts_file'):
            account_path = (config_path.parent / values['accounts_file']).resolve()
            account_config = tomllib.loads(account_path.read_text())
            if set(account_config) - {'accounts'}:raise ValueError('Unknown accounts file setting')
            values['_accounts'] = account_config.get('accounts', [])
        data = Path(os.environ.get("NEWSPAPER_DATA", values.get("data_dir", "/var/lib/newspaper"))).resolve()
        web = Path(os.environ.get("NEWSPAPER_WEB", values.get("web_dir", "/srv/newspaper"))).resolve()
        if data == web or data in web.parents or web in data.parents:
            raise ValueError("News data and published website must be separate directories")
        url = values.get("base_url", "http://localhost").rstrip("/")
        if not url.startswith(("http://", "https://")):
            raise ValueError("base_url must be an HTTP(S) URL")
        if values.get("retention_days", 30) != 30:
            raise ValueError("This project enforces a 30-day maximum retention")
        result = cls(values, data, web, url)
        result.validate()
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
        return Settings({**self.values, "sections": [s for s in self.sections if s["id"] in ids]},
                        self.data, self.web, self.base_url)

    def validate(self) -> None:
        from .schedule import validate_schedule
        from .accounts import accounts, account_mode
        if self.values.get('notifications',{}).get('mode','legacy') not in {'legacy','accounts'}:
            raise ValueError('Invalid notification mode')
        accounts(self)
        if account_mode(self) and not (self.values.get('project') or 'schedule' in self.values):
            raise ValueError('Account notifications require a project or schedule')
        if type(self.values.get('translation',{}).get('enabled',False)) is not bool:
            raise ValueError('Translation enabled must be boolean')
        if any(value for a in accounts(self) for value in a['options'].values()) and not self.values.get('translation',{}).get('enabled',False):
            raise ValueError('Bilingual subscriptions require translation.enabled')
        ZoneInfo(self.values.get('project', {}).get('timezone', 'UTC'))
        self.editions  # Validate group/section assignment before any work begins.
        discovery=self.values.get('discovery',{})
        for key,default,minimum,maximum in [('max_indexes_per_edition',32,1,128),('max_links_per_index',15,1,30),
                                          ('max_gap_queries',3,0,40),('max_previews_per_section',24,1,60)]:
            value=discovery.get(key,default)
            if type(value) is not int or not minimum<=value<=maximum:raise ValueError('Invalid discovery setting: '+key)
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
                if source.get('link_pattern'):
                    if not isinstance(source['link_pattern'],str) or len(source['link_pattern'])>500:raise ValueError('Invalid source link pattern')
                    re.compile(source['link_pattern'])
            if set(section.get('prompts',{}))-{'plan','select','edit','review','repair','compress','collect'}:raise ValueError('Unknown section prompt stage')
        home=self.values.get('homepage',{})
        if home.get('mode','groups') not in {'groups','sections','index'}:raise ValueError('Invalid homepage mode')
        if home.get('layout','tabs') not in {'tabs','columns'}:raise ValueError('Invalid homepage layout')
        if type(home.get('layout_switch',True)) is not bool:raise ValueError('Homepage layout_switch must be boolean')
        if not set(home.get('groups',[e['id'] for e in self.editions])) <= {e['id'] for e in self.values.get('editions',self.editions)}:raise ValueError('Unknown homepage group')
        if not set(home.get('sections',[])) <= {s['id'] for s in self.values.get('sections',[])}:raise ValueError('Unknown homepage section')

    def prompt_text(self, stage: str, sections: list[dict] | None = None) -> str:
        cfg=self.values.get('prompts',{})
        text=cfg.get(stage,'')
        file=cfg.get(stage+'_file')
        if file:
            text += '\n'+(Path(self.values.get('_project_dir', '.'))/file).read_text()
        for section in sections if sections is not None else self.sections:
            instruction=section.get('prompts',{}).get(stage,'')
            if instruction:text += '\n仅适用于板块 '+section['id']+'（'+section['name']+'）：'+instruction
        return text

    def prepare(self) -> None:
        for path in [self.data, self.data / "editions", self.data / "tmp", self.web, self.web / "releases"]:
            path.mkdir(parents=True, exist_ok=True)
