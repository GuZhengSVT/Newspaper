"""Discover recent links from configured publisher indexes through the DSH fetcher."""
from __future__ import annotations

import re
import subprocess
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from urllib.parse import urljoin
from xml.etree import ElementTree as ET

from .research import fetch_page, page_text, source_url


class IndexParser(HTMLParser):
    def __init__(self):
        super().__init__();self.links=[];self.href=None;self.parts=[]

    def handle_starttag(self, tag, attrs):
        if tag == 'a':self.href=dict(attrs).get('href');self.parts=[]

    def handle_data(self, text):
        if self.href is not None:self.parts.append(text)

    def handle_endtag(self, tag):
        if tag == 'a' and self.href is not None:
            self.links.append((self.href,' '.join(' '.join(self.parts).split())))
            self.href=None;self.parts=[]


def hinted_date(text: str) -> str | None:
    match=re.search(r'\b(20\d{2})[-/.](\d{1,2})[-/.](\d{1,2})(?!\d)',text)
    if match:
        try:return datetime(*(int(x) for x in match.groups())).date().isoformat()
        except ValueError:return None
    for pattern in ['%b %d, %Y','%B %d, %Y','%b %d %Y']:
        match=re.search(r'\b[A-Z][a-z]+\s+\d{1,2},?\s+20\d{2}\b',text)
        if match:
            try:return datetime.strptime(match.group(),pattern).date().isoformat()
            except ValueError:pass
    return None


def recent_links(content: str, base: str, window: dict, limit: int) -> list[dict]:
    candidates=[]
    try:
        root=ET.fromstring(content)
        if root.tag.rsplit('}',1)[-1].lower() not in {'rss','feed','rdf'}:
            raise ET.ParseError('Not a feed')
        for item in [*root.findall('.//{*}item'),*root.findall('.//{*}entry')]:
            title=item.findtext('{*}title','');link=item.find('{*}link')
            url=(link.get('href') or link.text or '') if link is not None else ''
            stamp=item.findtext('{*}pubDate') or item.findtext('{*}published') or item.findtext('{*}updated') or item.findtext('{*}date') or ''
            date=hinted_date(stamp)
            if not date:
                try:date=parsedate_to_datetime(stamp).date().isoformat()
                except (ValueError,TypeError):pass
            description=item.findtext('{*}description') or item.findtext('{*}summary') or ''
            candidates.append((url,title,date,title+' '+page_text(description,'html')[:500]))
    except ET.ParseError:
        parser=IndexParser();parser.feed(content)
        for url,title in parser.links:
            date=hinted_date(title)
            if date or (len(title)>=12 and re.search(r'/(blog|news|articles|press)/[^/#?]+',url)):
                candidates.append((url,title,date,title))
    # Dated cards take precedence over navigation links without dates.
    candidates.sort(key=lambda c:(c[2] is not None,c[2] or ''),reverse=True)
    known_dates={urljoin(base,c[0]):c[2] for c in candidates if c[2]}
    result={}
    for link,title,date,snippet in candidates:
        date=date or known_dates.get(urljoin(base,link))
        if date and not window['window_start'][:10]<=date<=window['date']:continue
        try:url=source_url(urljoin(base,link))
        except ValueError:continue
        if url==base or not title:continue
        result.setdefault(url,{'url':url,'title':title[:300],'snippet':snippet[:800],
                               'published_at':date,'discovery_index':base})
        if len(result)>=limit:break
    return list(result.values())


def discover(settings, window: dict, work) -> tuple[list[dict],dict]:
    cfg=settings.values.get('discovery',{})
    ids={s['id'] for s in settings.sections}
    indexes=[s for s in cfg.get('sources',[]) if s['section_id'] in ids][:min(8,cfg.get('max_indexes_per_edition',6))]
    limit=min(5,cfg.get('max_links_per_index',3))
    def read(item):
        try:
            page=fetch_page(settings,item['url'],work)
            return {'section_id':item['section_id'],'query':'publisher index','engines_ok':1,
                    'sources':recent_links(page['content'],item['url'],window,limit)}
        except (ValueError,KeyError,OSError):return None
        except subprocess.SubprocessError:return None
    with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(read,indexes))
    good=[r for r in results if r is not None]
    return good,{'discovery_indexes':len(indexes),'discovery_indexes_ok':len(good),
                 'discovery_links':sum(len(r['sources']) for r in good)}
