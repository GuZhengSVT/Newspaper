from __future__ import annotations

import copy
import hashlib
import json
import os
import tempfile
import threading
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch
from xml.etree import ElementTree

from newspaper.config import TZ, Settings, load_credentials
from newspaper.notify import deliver, enqueue
from newspaper.pipeline import generate, import_legacy
from newspaper.publish import build, markdown
from newspaper.retention import clean
from newspaper.storage import Store, atomic_json, expiry, lock, utcnow
from newspaper.validation import validate_report


class NewspaperTests(unittest.TestCase):
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

    def add(self, doc=None):
        doc = doc or self.doc
        directory = self.settings.data / "editions" / doc["date"]
        directory.mkdir(parents=True, exist_ok=True)
        atomic_json(directory / "report.json", doc)
        (directory / "report.md").write_text(markdown(doc))
        self.store.add(doc)
        self.store.db.execute("UPDATE editions SET status='published' WHERE day=?", (doc["date"],))
        self.store.db.commit()

    def test_valid_report(self):
        validate_report(self.doc, self.settings, [self.source], [], self.window)

    def test_fabricated_quote_rejected(self):
        self.doc["sections"][0]["articles"][0]["facts"][0]["evidence"][0]["quote"] = "Invented evidence quotation"
        with self.assertRaises(ValueError):
            validate_report(self.doc, self.settings, [self.source], [], self.window)

    def test_unknown_source_rejected(self):
        self.doc["sources"][0]["url"] = "https://example.test/invented"
        with self.assertRaises(ValueError):
            validate_report(self.doc, self.settings, [self.source], [], self.window)

    def test_old_article_rejected(self):
        self.doc["sections"][0]["articles"][0]["published_at"] = "2000-01-01T00:00:00+00:00"
        with self.assertRaises(ValueError):
            validate_report(self.doc, self.settings, [self.source], [], self.window)

    def test_duplicate_needs_follow_up(self):
        with self.assertRaises(ValueError):
            validate_report(self.doc, self.settings, [self.source], [{"id": "fixture|synthetic|event"}], self.window)

    def test_sections_must_match_configuration(self):
        self.doc["sections"][0]["id"] = "other"
        with self.assertRaises(ValueError):
            validate_report(self.doc, self.settings, [self.source], [], self.window)

    def test_template_escaping_and_downloads(self):
        self.doc["title"] = '<script>alert("xss")</script>'
        self.add()
        build(self.settings, self.store)
        page = (self.settings.web / "current/daily" / self.day / "index.html").read_text()
        self.assertNotIn('<script>alert("xss")</script>', page)
        self.assertIn('&lt;script&gt;', page)
        md = (self.settings.web / "current/downloads" / f"{self.day}.md").read_text()
        self.assertEqual(md, markdown(self.doc))
        self.assertFalse((self.settings.web / "current/raw.json").exists())
        ElementTree.parse(self.settings.web / "current/feed.xml")

    def test_atomic_build_failure_keeps_previous_release(self):
        self.add()
        build(self.settings, self.store)
        previous = (self.settings.web / "current").resolve()
        with patch("newspaper.publish.shutil.copytree", side_effect=OSError("fixture failure")):
            with self.assertRaises(OSError):
                build(self.settings, self.store)
        self.assertEqual((self.settings.web / "current").resolve(), previous)
        self.assertEqual(len(list((self.settings.web / "releases").iterdir())), 1)

    def test_retention_removes_all_artifacts_and_relations(self):
        self.add()
        old = copy.deepcopy(self.doc)
        old["date"] = (utcnow().date() - timedelta(days=30)).isoformat()
        old["summary"] = "EXPIRED-SYNTHETIC-MARKER-12345"
        self.add(old)
        enqueue(self.settings, self.store, old["date"])
        (self.settings.data / "editions" / old["date"] / "raw.json").write_text(old["summary"])
        orphan = self.settings.data / "tmp" / "crash"
        orphan.mkdir(); (orphan / "source.txt").write_text(old["summary"])
        # Simulate a previously served expired page and a superseded release.
        stale = self.settings.web / "releases/stale/daily" / old["date"]
        stale.mkdir(parents=True); (stale / "index.html").write_text(old["summary"])
        clean(self.settings, self.store)
        self.assertFalse((self.settings.data / "editions" / old["date"]).exists())
        self.assertFalse(orphan.exists())
        self.assertFalse((self.settings.web / "current/daily" / old["date"]).exists())
        self.assertFalse(self.store.db.execute("SELECT 1 FROM deliveries WHERE day=?", (old["date"],)).fetchone())
        self.assertFalse(self.store.db.execute("SELECT 1 FROM events WHERE day=?", (old["date"],)).fetchone())
        for p in self.settings.web.rglob("*"):
            if p.is_file(): self.assertNotIn(old["summary"], p.read_text())
        self.assertNotIn(old["summary"].encode(), (self.settings.data / "newspaper.sqlite").read_bytes())

    def test_expiry_boundary_is_not_extended_by_rebuild(self):
        due = expiry(self.day)
        self.add()
        self.assertEqual(len(self.store.editions(due - 0.01)), 1)
        clean(self.settings, self.store, due)
        self.assertEqual(len(self.store.editions(due)), 0)

    def test_notification_retry_does_not_repeat_success(self):
        self.add(); enqueue(self.settings, self.store, self.day)
        with patch("newspaper.notify.readiness", return_value=True), patch("newspaper.notify.send") as send:
            first = deliver(self.settings, self.store)
            second = deliver(self.settings, self.store)
        self.assertEqual(first["sent"], 2)
        self.assertEqual(second["sent"], 0)
        self.assertEqual(send.call_count, 2)

    def test_notification_failure_redacts_token(self):
        self.add(); enqueue(self.settings, self.store, self.day)
        with patch("newspaper.notify.readiness", return_value=True), patch("newspaper.notify.send", side_effect=RuntimeError("secret-token")):
            result = deliver(self.settings, self.store)
        self.assertEqual(result["failed"], 2)
        self.assertNotIn("secret-token", str([dict(r) for r in self.store.db.execute("SELECT * FROM deliveries")]))

    def test_unconfigured_notifications_do_not_exhaust_retries(self):
        self.add(); enqueue(self.settings, self.store, self.day)
        with patch("newspaper.notify.readiness", return_value=False):
            self.assertEqual(deliver(self.settings, self.store)["unconfigured"], 2)
        self.assertEqual(self.store.db.execute("SELECT max(attempts) FROM deliveries").fetchone()[0], 0)

    def test_pending_sections_skip_model(self):
        settings = Settings({**self.settings.values, "sections": []}, self.settings.data, self.settings.web, self.settings.base_url)
        with patch("newspaper.pipeline.Model") as model:
            self.assertEqual(generate(settings, self.store)["state"], "awaiting_sections")
            model.assert_not_called()

    def test_pipeline_publishes_only_after_review(self):
        collection = {"section_id": "fixture", "sources": [self.source], "notes": []}
        moment = datetime.fromisoformat(self.day + "T12:05:00+08:00")
        with patch("newspaper.pipeline.utcnow", return_value=moment), patch("newspaper.pipeline.gather", return_value=collection), \
             patch("newspaper.pipeline.Model.request", side_effect=[copy.deepcopy(self.doc), {"ok": True, "problems": [], "research_queries": []}]), \
             patch("newspaper.notify.readiness", return_value=False):
            result = generate(self.settings, self.store)
        self.assertEqual(result["state"], "published")
        self.assertTrue(self.store.published(self.day))
        self.assertEqual(list((self.settings.data / "tmp").iterdir()), [])
        self.assertTrue((self.settings.web / "current/daily" / self.day / "index.html").is_file())

    def test_review_rejection_does_not_publish_or_notify(self):
        collection = {"section_id": "fixture", "sources": [self.source], "notes": []}
        moment = datetime.fromisoformat(self.day + "T12:05:00+08:00")
        failure = {"ok": False, "problems": ["Unsupported fact"], "research_queries": []}
        with patch("newspaper.pipeline.utcnow", return_value=moment), patch("newspaper.pipeline.gather", return_value=collection), \
             patch("newspaper.pipeline.Model.request", side_effect=[copy.deepcopy(self.doc), failure, copy.deepcopy(self.doc), failure]):
            with self.assertRaises(ValueError): generate(self.settings, self.store)
        self.assertFalse(self.store.published(self.day))
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM deliveries").fetchone()[0], 0)
        self.assertEqual(list((self.settings.data / "tmp").iterdir()), [])

    def test_legacy_import_preserves_original_bytes(self):
        source = self.root / "legacy"; source.mkdir()
        path = source / (self.day + " 新闻总结.md")
        original = '# Synthetic archive\n\n<script>alert("unsafe")</script>\n'
        path.write_text(original)
        self.assertEqual(import_legacy(self.settings, self.store, source)["imported"], 1)
        copied = self.settings.data / "editions" / self.day / "report.md"
        self.assertEqual(copied.read_bytes(), path.read_bytes())
        page = (self.settings.web / "current/daily" / self.day / "index.html").read_text()
        self.assertNotIn('<script>alert("unsafe")</script>', page)

    def test_lock_excludes_other_jobs(self):
        with lock(self.settings):
            with self.assertRaises(BlockingIOError):
                with lock(self.settings): pass

    def test_generator_can_wait_for_short_cleanup(self):
        acquired = threading.Event()
        def wait_for_lock():
            with lock(self.settings, wait_seconds=1): acquired.set()
        with lock(self.settings):
            worker = threading.Thread(target=wait_for_lock)
            worker.start()
            self.assertFalse(acquired.wait(0.05))
        worker.join(timeout=2)
        self.assertTrue(acquired.is_set())

    def test_retention_deletes_data_even_when_renderer_fails(self):
        old = copy.deepcopy(self.doc)
        old["date"] = (utcnow().date() - timedelta(days=30)).isoformat()
        self.add(old)
        enqueue(self.settings, self.store, old["date"])
        with patch("newspaper.retention.build", side_effect=OSError("fixture renderer failure")):
            with self.assertRaises(RuntimeError): clean(self.settings, self.store)
        self.assertFalse((self.settings.data / "editions" / old["date"]).exists())
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM editions").fetchone()[0], 0)
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM deliveries").fetchone()[0], 0)

    def test_inaccessible_legacy_input_is_not_a_success(self):
        with self.assertRaises(ValueError):
            import_legacy(self.settings, self.store, self.root / "missing-directory")

    def test_credentials_are_literal_not_executable(self):
        path = self.root / "secrets"
        path.write_text('NEWSPAPER_TEST_SECRET="$(touch /nonexistent/do-not-create)"\n')
        with patch.dict(os.environ, {}, clear=True):
            load_credentials(path)
            self.assertEqual(os.environ["NEWSPAPER_TEST_SECRET"], "$(touch /nonexistent/do-not-create)")


if __name__ == "__main__": unittest.main()
