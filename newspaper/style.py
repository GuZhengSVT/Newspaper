"""Bounds for public article bodies; evidence quotations and internal notes are excluded."""
from __future__ import annotations
import copy


def style_issues(doc: dict, settings=None) -> list[dict]:
    issues = []
    for section in doc['sections']:
        for article in section['articles']:
            if section['id'] == 'science':
                paragraphs, characters = 6, 1100
            elif article.get('importance') == '重大':
                paragraphs, characters = 4, 650
            else:
                paragraphs, characters = 3, 420
            fmt=next((s.get('format',{}) for s in settings.sections if s['id']==section['id']),{}) if settings else {}
            major=article.get('importance')=='重大'
            characters=fmt.get('max_major_characters' if major else 'max_characters',characters)
            paragraphs=fmt.get('max_major_paragraphs' if major else 'max_paragraphs',paragraphs)
            actual = sum(len(f['text']) for f in article['facts'])
            if actual > characters:
                issues.append({'event_key': article['event_key'], 'max_paragraphs': paragraphs,
                    'max_characters': characters, 'paragraphs': len(article['facts']), 'characters': actual})
    return issues


def compact_articles(doc: dict, patch: dict, allowed: set[str]) -> dict:
    """Shorten fact text while preserving its original evidence and verification."""
    result=copy.deepcopy(doc)
    articles={a['event_key']:a for s in result['sections'] for a in s['articles']}
    seen=set()
    for item in patch['articles']:
        key=item['event_key']
        if key not in allowed or key in seen:raise ValueError('Compaction changed an unrelated event')
        seen.add(key);article=articles[key];facts=[];indexes=set()
        for edit in item['facts']:
            index=edit['index'];text=edit['text']
            if (type(index) is not int or not 0<=index<len(article['facts']) or index in indexes
                or not isinstance(text,str) or not text.strip()):
                raise ValueError('Invalid compacted fact')
            indexes.add(index);fact=copy.deepcopy(article['facts'][index]);fact['text']=text.strip();facts.append(fact)
        if not facts:raise ValueError('Compaction cannot delete the article')
        article['facts']=facts;article['summary']=facts[0]['text'][:500]
    if seen!=allowed:raise ValueError('Compaction omitted a requested event')
    all_articles=[a for s in result['sections'] for a in s['articles']]
    result['summary']='本期收录'+str(len(all_articles))+'条新闻。'
    result['highlights']=[a['title'] for a in all_articles[:5]]
    return result
