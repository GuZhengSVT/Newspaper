"""Bounds for public article bodies; evidence quotations and internal notes are excluded."""
from __future__ import annotations


def style_issues(doc: dict) -> list[dict]:
    issues = []
    for section in doc['sections']:
        for article in section['articles']:
            if section['id'] == 'science':
                paragraphs, characters = 6, 1100
            elif article.get('importance') == '重大':
                paragraphs, characters = 4, 650
            else:
                paragraphs, characters = 3, 420
            actual = sum(len(f['text']) for f in article['facts'])
            if len(article['facts']) > paragraphs or actual > characters:
                issues.append({'event_key': article['event_key'], 'max_paragraphs': paragraphs,
                    'max_characters': characters, 'paragraphs': len(article['facts']), 'characters': actual})
    return issues
