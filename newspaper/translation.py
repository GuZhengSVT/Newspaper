"""Translate and validate sentence pairs for synchronous and deferred publication."""
from __future__ import annotations

import re
import time


def sentence_units(article: dict) -> list[str]:
    paragraphs = list(dict.fromkeys(f['text'].strip() for f in article['facts'] if f['text'].strip()))
    result = []
    for paragraph in paragraphs or [article['summary']]:
        # Punctuation inside a title such as 《BanG Dream! It's MyGO!!!!!》 is not a sentence boundary.
        pairs = {'《': '》', '「': '」', '『': '』', '“': '”', '‘': '’', '（': '）', '(': ')', '【': '】', '[': ']', '"': '"'}
        stack = [];start = 0;index = 0
        while index < len(paragraph):
            char = paragraph[index]
            if stack and char == stack[-1]:stack.pop()
            elif char in pairs and pairs[char] in paragraph[index + 1:]:stack.append(pairs[char])
            elif not stack and char in '。！？!?\n':
                end = index + 1
                while end < len(paragraph) and paragraph[end] in '。！？!?\n”’"」』）)]':end += 1
                if paragraph[start:end].strip():result.append(paragraph[start:end].strip())
                start = end;index = end;continue
            index += 1
        if paragraph[start:].strip():result.append(paragraph[start:].strip())
    return result


def legacy_sentence_units(article: dict) -> list[str]:
    # Read existing stored pairs without invalidating already published archives.
    paragraphs = list(dict.fromkeys(f['text'].strip() for f in article['facts'] if f['text'].strip()))
    return [m.group().strip() for paragraph in paragraphs or [article['summary']]
            for m in re.finditer(r'.+?(?:[。！？!?]+[”’"」』）)]*|\n+|$)', paragraph, re.S) if m.group().strip()]


def validate_bilingual(article: dict, *, required: bool = False) -> None:
    value = article.get('bilingual')
    if value is None:
        if required:raise ValueError('Reviewed bilingual content is missing')
        return
    if not isinstance(value, dict) or set(value) != {'title_en', 'sentences'}:
        raise ValueError('Invalid bilingual content')
    english = [value['title_en']]
    pairs = value['sentences']
    if (not isinstance(pairs, list) or not pairs
        or any(not isinstance(p, dict) or set(p) != {'en', 'zh'} for p in pairs)
        or [p['zh'] for p in pairs] not in [sentence_units(article), legacy_sentence_units(article)]):
        raise ValueError('Bilingual sentences do not align with the approved Chinese text')
    english += [p['en'] for p in pairs]
    if any(not isinstance(s, str) or not s.strip() or len(s) > 12000 or '\n' in s
           or not re.search(r'[A-Za-z]', s) or re.search(r'[\u3400-\u9fff]', s) for s in english):
        raise ValueError('Invalid English translation')


def paragraph_pairs(article: dict, paragraphs: list[str]) -> list[dict]:
    """Group the stored sentence pairs into the same paragraphs as Chinese-only reading."""
    validate_bilingual(article, required=True)
    pairs=iter(article['bilingual']['sentences']);result=[]
    compact=lambda text: re.sub(r'\s+', '', text)
    for paragraph in paragraphs:
        english=[];chinese=''
        while compact(chinese) != compact(paragraph):
            pair=next(pairs)
            chinese+=pair['zh'];english.append(pair['en'])
            if not compact(paragraph).startswith(compact(chinese)):
                raise ValueError('Bilingual paragraph alignment mismatch')
        result.append({'en':' '.join(english),'zh':paragraph})
    if next(pairs,None) is not None:raise ValueError('Unmatched bilingual sentences')
    return result


def validate_dates(chinese: str, english: str) -> None:
    """Reject changed explicit calendar dates before model opinions can approve them."""
    dates = set();month = None
    for match in re.finditer(r'(?:(\d{1,2})月)?(\d{1,2})日', chinese):
        if match[1]:month = int(match[1])
        dates.add((month, int(match[2])))
    if not dates:return
    months = ['January', 'February', 'March', 'April', 'May', 'June',
              'July', 'August', 'September', 'October', 'November', 'December']
    names = {name.casefold(): i for i, name in enumerate(months, 1)}
    names.update({name[:3].casefold(): i for i, name in enumerate(months, 1)})
    alternatives = '|'.join(sorted(names, key=len, reverse=True))
    matches = []
    for match in re.finditer(r'\b(' + alternatives + r')\.?\s+(\d{1,2})(?:st|nd|rd|th)?\b', english, re.I):
        matches.append((names[match[1].casefold()], int(match[2])))
    for match in re.finditer(r'\b(\d{1,2})(?:st|nd|rd|th)?\s+(' + alternatives + r')\b', english, re.I):
        matches.append((names[match[2].casefold()], int(match[1])))
    for match in re.finditer(r'\b\d{4}-(\d{2})-(\d{2})\b', english):
        matches.append((int(match[1]), int(match[2])))
    for month, day in matches:
        if (month, day) not in dates and (None, day) not in dates:
            raise ValueError(f'English calendar date {month:02d}-{day:02d} differs from the Chinese source')



def translate(doc: dict, client, *, diagnostics: list | None = None, on_article_translated=None,
              should_translate=None, on_article_failed=None) -> int:
    """Translate each unfinished article once; validate structure without a model reviewer."""
    count = 0
    for section in doc.get('sections', []):
        for article in section['articles']:
            if article.get('bilingual'):
                validate_bilingual(article, required=True)
                continue
            if should_translate is not None and not should_translate(article):
                continue
            units = sentence_units(article)
            source = {'title_zh': article['title'], 'sentences': [
                {'id': i, 'zh': text} for i, text in enumerate(units)]}
            try:
                draft = client.request('translate', source)
                rows = draft.get('sentences', [])
                if (not isinstance(rows, list) or len(rows) != len(units)
                    or any(not isinstance(r, dict) or type(r.get('id')) is not int for r in rows)
                    or [r['id'] for r in rows] != list(range(len(units)))):
                    raise ValueError('Translation omitted, duplicated or reordered sentences')
                value = {'title_en': draft.get('title_en'), 'sentences': [
                    {'en': r.get('en'), 'zh': zh} for r, zh in zip(rows, units)]}
                validate_bilingual({**article, 'bilingual': value}, required=True)
                validate_dates(article['title'], value['title_en'])
                for pair in value['sentences']:validate_dates(pair['zh'], pair['en'])
            except Exception as exc:
                if diagnostics is not None:
                    diagnostics.append({'section_id': section['id'], 'event_key': article.get('event_key'),
                        'attempt': 1, 'stage': 'translate', 'error_type': type(exc).__name__,
                        'reason': str(exc)[:600] if isinstance(exc, (ValueError, KeyError, TypeError))
                                  else type(exc).__name__})
                if on_article_failed is not None:
                    on_article_failed(article, exc)
                    continue
                raise
            article['bilingual'] = value
            count += 1
            if on_article_translated:on_article_translated(article)
    return count


def translation_mode(settings) -> str:
    config = settings.values.get('translation', {})
    return config.get('publish', 'together') if config.get('enabled', False) else 'disabled'


def queue_translation(store, day: str) -> None:
    store.db.execute('''CREATE TABLE IF NOT EXISTS pending_translations (
        day TEXT PRIMARY KEY REFERENCES editions(day) ON DELETE CASCADE,
        needs_build INTEGER NOT NULL DEFAULT 0)''')
    store.db.execute('''CREATE TABLE IF NOT EXISTS translation_retries (
        day TEXT NOT NULL REFERENCES pending_translations(day) ON DELETE CASCADE,
        event_key TEXT NOT NULL, attempts INTEGER NOT NULL, next_attempt REAL NOT NULL,
        error_type TEXT NOT NULL, PRIMARY KEY(day,event_key))''')
    store.db.execute('INSERT OR IGNORE INTO pending_translations(day) VALUES(?)', (day,))


def resume_translations(settings, store, *, day: str | None = None, manual: bool = False) -> dict:
    """Continue retained editions under the pipeline lock, preserving their delivery records."""
    from .model import Model, ModelBudgetReached, ModelDeferred, provider_failure
    from .publish import build, markdown
    from .storage import atomic_json, utcnow

    result = {'translated_articles': 0, 'failed': 0, 'deferred': 0, 'budget_blocked': [], 'rebuilt': False}
    mode = translation_mode(settings)
    if mode == 'disabled' and not manual:
        return result
    db = store.db
    now = utcnow().timestamp()
    if manual:
        entries = db.execute("SELECT day FROM editions WHERE status='published' AND legacy=0 AND expires_at>? ORDER BY day",
                             (now,))
        for key, in entries.fetchall():
            if day is not None and key != day and not key.startswith(day + '/'):
                continue
            queue_translation(store, key)
        db.commit()
    if not db.execute("SELECT 1 FROM sqlite_master WHERE name='pending_translations'").fetchone():
        return result
    entries = db.execute('''SELECT p.day,p.needs_build FROM pending_translations p JOIN editions e USING(day)
                            WHERE e.status='published' AND e.expires_at>? ORDER BY p.day''', (now,)).fetchall()
    changed = []
    complete = []
    provider_deferred = False
    retry_delays = (300, 1800, 7200)
    attempts_this_run = 0
    deadline = time.monotonic() + 20
    for key, dirty in entries:
        if manual and day is not None and key != day and not key.startswith(day + '/'):
            continue
        doc = store.document(key)
        articles = [article for section in doc.get('sections', []) for article in section['articles']]
        directory = settings.data / 'editions' / key
        retries = {row['event_key']: row['attempts'] for row in db.execute('''SELECT * FROM translation_retries
                   WHERE day=? AND (attempts>? OR next_attempt>?)''', (key, len(retry_delays), now))}
        budget_blocked = False

        def should_translate(article):
            nonlocal provider_deferred, attempts_this_run, budget_blocked
            if not manual and (attempts_this_run >= 2 or time.monotonic() >= deadline):
                result['deferred'] += 1
                return False
            if provider_deferred or budget_blocked or (article['event_key'] in retries and not manual):
                result['deferred'] += 1
                return False
            try:
                client.check_budget()
                client.check_backoff()
            except ModelBudgetReached:
                budget_blocked = True
                result['budget_blocked'].append(key)
                result['deferred'] += 1
                return False
            except ModelDeferred:
                provider_deferred = True
                result['deferred'] += 1
                return False
            attempts_this_run += 1
            return True

        def store_article(article):
            nonlocal dirty
            db.execute('UPDATE pending_translations SET needs_build=1 WHERE day=?', (key,))
            db.commit()
            dirty = True
            atomic_json(directory / 'report.json', doc)
            db.execute('DELETE FROM translation_retries WHERE day=? AND event_key=?', (key, article['event_key']))
            db.commit()

        def store_failure(article, error):
            nonlocal provider_deferred, budget_blocked
            if isinstance(error, ModelBudgetReached):
                budget_blocked = True
                result['budget_blocked'].append(key)
                result['deferred'] += 1
                return
            if isinstance(error, ModelDeferred):
                provider_deferred = True
                result['deferred'] += 1
                return
            if provider_failure(error):
                provider_deferred = True
                result['failed'] += 1
                return
            event_key = article['event_key']
            current = db.execute('SELECT attempts FROM translation_retries WHERE day=? AND event_key=?',
                                 (key, event_key)).fetchone()
            attempts = (current[0] if current else 0) + 1
            wait = retry_delays[min(attempts - 1, len(retry_delays) - 1)]
            db.execute('INSERT OR REPLACE INTO translation_retries VALUES(?,?,?,?,?)',
                       (key, event_key, attempts, utcnow().timestamp() + wait, type(error).__name__))
            db.commit()
            result['failed'] += 1

        if any(not a.get('bilingual') for a in articles):
            client = Model(settings, store=store, edition_key=key, time_limited=not manual)
            result['translated_articles'] += translate(doc, client, should_translate=should_translate,
                on_article_translated=store_article, on_article_failed=store_failure)
        if dirty:
            (directory / 'report.md').write_text(markdown(doc, settings))
            changed.append((key,))
        if all(a.get('bilingual') for a in articles):
            complete.append((key,))
    if changed:
        result.update(build(settings, store), rebuilt=True)
        db.executemany('UPDATE pending_translations SET needs_build=0 WHERE day=?', changed)
    if complete:
        db.executemany('DELETE FROM pending_translations WHERE day=?', complete)
    if changed or complete:
        db.commit()
    return result
