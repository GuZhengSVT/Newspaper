"""Produce reviewed sentence pairs once, before publication; never translate in readers."""
from __future__ import annotations

import re


def sentence_units(article: dict) -> list[str]:
    paragraphs = list(dict.fromkeys(f['text'].strip() for f in article['facts'] if f['text'].strip()))
    result = []
    for paragraph in paragraphs or [article['summary']]:
        # Keep closing quotation marks with their sentence. Decimal points and abbreviations stay intact.
        result.extend(m.group().strip() for m in re.finditer(r'.+?(?:[。！？!?]+[”’"」』）)]*|\n+|$)', paragraph, re.S)
                      if m.group().strip())
    return result


def validate_bilingual(article: dict, *, required: bool = False) -> None:
    value = article.get('bilingual')
    if value is None:
        if required:raise ValueError('Reviewed bilingual content is missing')
        return
    if not isinstance(value, dict) or set(value) != {'title_en', 'sentences'}:
        raise ValueError('Invalid bilingual content')
    english = [value['title_en']]
    pairs = value['sentences']
    if (not isinstance(pairs, list) or not pairs or len(pairs) != len(sentence_units(article))
        or any(not isinstance(p, dict) or set(p) != {'en', 'zh'} for p in pairs)
        or [p['zh'] for p in pairs] != sentence_units(article)):
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


def translate(doc: dict, client) -> int:
    """Per-article bounded translation + independent review; commit no partial article."""
    count = 0
    for section in doc.get('sections', []):
        for article in section['articles']:
            if article.get('bilingual'):
                validate_bilingual(article, required=True)
                continue
            units = sentence_units(article)
            source = {'title_zh': article['title'], 'sentences': [
                {'id': i, 'zh': text} for i, text in enumerate(units)]}
            feedback = ''
            for attempt in range(2):
                try:
                    draft = client.request('translate', {**source, 'feedback': feedback})
                    rows = draft.get('sentences', [])
                    if (not isinstance(rows, list) or len(rows) != len(units)
                        or any(not isinstance(r, dict) or type(r.get('id')) is not int for r in rows)
                        or [r['id'] for r in rows] != list(range(len(units)))):
                        raise ValueError('Translation omitted, duplicated or reordered sentences')
                    value = {'title_en': draft.get('title_en'), 'sentences': [
                        {'en': r.get('en'), 'zh': zh} for r, zh in zip(rows, units)]}
                    validate_bilingual({**article, 'bilingual': value}, required=True)
                    review = client.request('translate-review', {**source, 'translation': draft})
                    verdicts = review.get('sentences', [])
                    if (review.get('title_ok') is not True or review.get('ok') is not True or review.get('problems',[]) != []
                        or not isinstance(verdicts, list) or len(verdicts) != len(units)
                        or any(not isinstance(r, dict) or type(r.get('id')) is not int for r in verdicts)
                        or [r['id'] for r in verdicts] != list(range(len(units)))
                        or any(r.get('ok') is not True or r.get('problems') != [] for r in verdicts)):
                        raise ValueError('Translation review rejected alignment, fidelity or fluency: ' +
                                         str(review)[:1200])
                except (ValueError, KeyError, TypeError) as exc:
                    if attempt:raise ValueError('Bilingual translation failed review') from exc
                    feedback = str(exc)[:600]
                    continue
                article['bilingual'] = value
                count += 1
                break
    return count
