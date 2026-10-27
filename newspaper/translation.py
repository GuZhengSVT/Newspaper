"""Translate sentence pairs once, before publication; never translate in readers."""
from __future__ import annotations

import re


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



def translate(doc: dict, client, *, diagnostics: list | None = None, on_article_translated=None) -> int:
    """Translate each unfinished article once; validate structure without a model reviewer."""
    count = 0
    for section in doc.get('sections', []):
        for article in section['articles']:
            if article.get('bilingual'):
                validate_bilingual(article, required=True)
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
                raise
            article['bilingual'] = value
            count += 1
            if on_article_translated:on_article_translated(article)
    return count
