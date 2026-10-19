"""Reader-facing views shared by HTML and Markdown; evidence stays in report JSON."""
from __future__ import annotations

from datetime import datetime
from urllib.parse import urlsplit

from .config import TZ

SOURCE_NAMES = {
    'prtimes.jp': 'PR TIMES', 'xinhuanet.com': '新华社', 'news.cn': '新华社',
    'gov.cn': '中国政府网', 'cas.cn': '中国科学院', 'nature.com': 'Nature',
    'science.org': 'Science', 'arxiv.org': 'arXiv', 'nasa.gov': 'NASA',
    'esa.int': '欧洲航天局', 'deepseek.com': 'DeepSeek', 'anthropic.com': 'Anthropic',
    'reuters.com': '路透社', 'apnews.com': '美联社', 'store.steampowered.com': 'Steam',
}


def source_label(url: str) -> str:
    host = (urlsplit(url).hostname or '').removeprefix('www.')
    for domain, label in SOURCE_NAMES.items():
        if host == domain or host.endswith('.' + domain):
            return label
    return host


def display_time(value: str, pattern: str = '%m月%d日 %H:%M') -> str:
    moment = datetime.fromisoformat(value)
    return moment.astimezone(TZ).strftime(pattern)


def report_view(doc: dict, key: str) -> dict:
    sources = {s['id']: s for s in doc.get('sources', [])}
    prefix = doc.get('edition', 'daily')
    sections = []
    for si, section in enumerate(doc.get('sections', []), 1):
        if not section['articles']:
            continue
        articles = []
        for ai, article in enumerate(section['articles'], 1):
            references = {}
            for fact in article['facts']:
                for evidence in fact['evidence']:
                    source = sources[evidence['source_id']]
                    references.setdefault(source['url'], {
                        'url': source['url'], 'label': source_label(source['url']), 'title': source['title'],
                    })
            paragraphs = list(dict.fromkeys(f['text'].strip() for f in article['facts'] if f['text'].strip()))
            articles.append({**article, 'anchor': f'{prefix}-s{si}-a{ai}',
                'paragraphs': paragraphs or [article['summary']], 'references': list(references.values()),
                'time': display_time(article['published_at'])})
        sections.append({'name': section['name'], 'anchor': f'{prefix}-s{si}', 'articles': articles})
    return {'key': key, 'date': doc['date'], 'title': doc['title'], 'sections': sections,
            'count': sum(len(s['articles']) for s in sections),
            'window': (display_time(doc['window_start']) + ' — ' + display_time(doc['window_end']))
                      if doc.get('window_start') else ''}
