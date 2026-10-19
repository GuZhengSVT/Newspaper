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

from .research import fetch_page, page_text, source_url, publication_time


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
            precise = None
            try:
                stamp_time = datetime.fromisoformat(stamp.replace('Z', '+00:00'))
                if stamp_time.tzinfo is not None:precise = stamp_time.isoformat()
            except ValueError:
                try:
                    stamp_time = parsedate_to_datetime(stamp)
                    if stamp_time.tzinfo is not None:precise = stamp_time.isoformat()
                except (ValueError,TypeError):pass
            candidates.append((url,title,date,title+' '+page_text(description,'html')[:500],precise))
    except ET.ParseError:
        if re.search(r'<(?:rss|feed|rdf:RDF)\b',content[:1000],re.I):
            raise ValueError('Invalid or truncated publisher feed')
        parser=IndexParser();parser.feed(content)
        for url,title in parser.links:
            date=hinted_date(title) or hinted_date(url)
            if date or (len(title)>=8 and re.search(r'/(blog|news|articles?|press)/[^/#?]+|/20\d{2}/\d{1,2}/[^/#?]+|/\d+/\d+/\d+\.htm',url)):
                candidates.append((url,title,date,title,None))
    # Dated cards take precedence over navigation links without dates.
    candidates.sort(key=lambda c:(c[2] is not None,c[2] or ''),reverse=True)
    known_dates={urljoin(base,c[0]):c[2] for c in candidates if c[2]}
    result={}
    for link,title,date,snippet,precise in candidates:
        date=date or known_dates.get(urljoin(base,link))
        if date and not window['window_start'][:10]<=date<=window['date']:continue
        try:url=source_url(urljoin(base,link))
        except ValueError:continue
        if url==base or not title:continue
        result.setdefault(url,{'url':url,'title':title[:300],'snippet':snippet[:800],
                               'published_at':precise or date,'discovery_index':base,
                               **({'publisher_published_at':precise} if precise else {})})
        if len(result)>=limit:break
    return list(result.values())


def discover(settings, window: dict, work) -> tuple[list[dict],dict]:
    cfg=settings.values.get('discovery',{})
    ids={s['id'] for s in settings.sections}
    indexes=[s for s in cfg.get('sources',[]) if s['section_id'] in ids]
    indexes += [{**source,'section_id':section['id']} for section in settings.sections for source in section.get('sources',[])]
    indexes=list({(s['section_id'],s['url']):s for s in indexes}.values())
    budget=min(24,cfg.get('max_indexes_per_edition',12))
    if sum(bool(s.get('required')) for s in indexes)>budget:raise ValueError('Required sources exceed discovery budget')
    indexes=sorted(indexes,key=lambda s:not s.get('required',False))[:budget]
    limit=min(30,cfg.get('max_links_per_index',15))
    def read(item):
        try:
            page=fetch_page(settings,item['url'],work,max_chars=2000000)
            if item.get('kind')=='page':
                text=page_text(page['content'],page['kind'])
                if len(text)<100:raise ValueError('Required page has no usable content')
                links=[{'url':item['url'],'title':item.get('name',item['url']), 'snippet':text[:6000],
                        'published_at':publication_time(page['content']), 'discovery_index':item['url']}]
            else:links=recent_links(page['content'],item['url'],window,limit)
            return {'section_id':item['section_id'],'query':'publisher index','index_url':item['url'],
                    'required':item.get('required',False),'engines_ok':int(bool(links)),'sources':links,'index_status':'links' if links else 'no_recent_links'}
        except (ValueError,KeyError,OSError,subprocess.SubprocessError) as exc:
            return {'section_id':item['section_id'],'query':'publisher index','index_url':item['url'],
                    'required':item.get('required',False),'engines_ok':0,'sources':[],'index_status':type(exc).__name__}
    with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(read,indexes))
    failed=[r['index_url'] for r in results if r.get('required') and r['index_status'] not in {'links','no_recent_links'}]
    if failed:raise ValueError('Required sources could not be read: '+', '.join(failed))
    good=[r for r in results if r['engines_ok']]
    return results,{'discovery_indexes':len(indexes),'discovery_indexes_ok':len(good),
                 'discovery_links':sum(len(r['sources']) for r in good),
                 'publisher_indexes':[{'section_id':r['section_id'],'url':r['index_url'],
                                       'status':r['index_status'],'links':len(r['sources'])} for r in results]}
