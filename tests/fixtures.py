"""Shared synthetic data and storage fixtures; no test cases or real deliveries."""
import copy
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from newspaper.config import Settings
from newspaper.publish import markdown
from newspaper.storage import Store, atomic_json, edition_key, utcnow


class NewspaperCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.day = utcnow().date().isoformat()
        self.settings = Settings({"model": {"provider": "fixture", "id": "fixture-model", "base_url": "https://example.test/v1"},
            "sections": [{"id": "fixture", "name": "测试板块"}],
            "notifications": {"email": {"enabled": True}, "telegram": {"enabled": True}}},
            self.root / "data", self.root / "web", "https://example.test")
        self.store = Store(self.settings)
        self.source = {"id": "s1", "url": "https://example.test/source", "title": "Synthetic fixture source",
                       "published_at": self.day + "T10:00:00+08:00", "content": "This is synthetic evidence for a test, not actual news."}
        self.window = {"date": self.day, "window_start": (utcnow().date() - timedelta(days=1)).isoformat() + "T12:00:00+08:00",
                       "window_end": self.day + "T12:00:00+08:00"}
        self.doc = {"schema_version": 1, **self.window, "generated_at": self.day + "T12:05:00+08:00",
                    "title": "合成测试日报", "summary": "仅用于离线测试。", "highlights": ["Synthetic fixture"],
                    "sources": [{k: self.source[k] for k in ["id", "url", "title", "published_at"]}],
                    "sections": [{"id": "fixture", "name": "测试板块", "empty_reason": "", "articles": [{
                        "event_key": "fixture|synthetic|event", "title": "合成事件", "summary": "测试事实",
                        "published_at": self.source["published_at"], "follow_up": False, "new_facts": "",
                        "verification": "single_source", "facts": [{"text": "离线测试文本", "evidence": [
                            {"source_id": "s1", "quote": "This is synthetic evidence for a test"}]}]}]}]}

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def issue(self, slug):
        doc = copy.deepcopy(self.doc)
        doc.update(schema_version=2, edition=slug, title=slug)
        a = doc['sections'][0]['articles'][0]
        a.update(importance='重要', reliability='A', entities=['Fixture Product'], verification='official_statement')
        for f in a['facts']:f.update(kind='fact',verification='official_statement')
        return doc

    def save(self, doc=None, *, task_id=None):
        doc = doc or self.doc
        key = edition_key(doc)
        directory = self.settings.data / "editions" / key
        directory.mkdir(parents=True, exist_ok=True)
        atomic_json(directory / "report.json", doc)
        (directory / "report.md").write_text(markdown(doc))
        self.store.add(doc, task_id=task_id)
        self.store.db.execute("UPDATE editions SET status='published' WHERE day=?", (key,))
        self.store.db.commit()
        return doc

    add = save


class ProjectCase(NewspaperCase):
    def setUp(self):
        super().setUp()
        self.settings_base = self.settings.values
        self.now = datetime.fromisoformat(self.day + 'T12:05:00+08:00')

    def make_settings(self, schedules=None):
        sections=[{'id':'fixture','name':'First'},{'id':'other','name':'Second'}]
        if schedules:
            for section,schedule in zip(sections,schedules):section['schedule']=schedule
        return Settings({**self.settings_base,'project':{'name':'Test news','timezone':'Asia/Shanghai'},
                         'schedule':{'period':'daily','time':'12:00'},'sections':sections,
                         'editions':[{'id':'first','name':'First group','sections':['fixture']},
                                     {'id':'second','name':'Second group','sections':['other']}]},
                        self.root/'data',self.root/'web','https://example.test')

    def save(self, job, window):
        doc = self.issue(job['id'])
        doc.update(window, title=job['name'])
        section = doc['sections'][0]
        section.update(id=job['sections'][0], name=job['name'])
        article = section['articles'][0]
        article['event_key'] = 'fixture|release|' + job['id']
        article['published_at'] = (datetime.fromisoformat(window['window_end']) - timedelta(hours=1)).isoformat()
        return super().save(doc, task_id=job['task_id'])
