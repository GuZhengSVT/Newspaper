"""Discover recent links from configured publisher indexes through the DSH fetcher."""
from __future__ import annotations

import re
import json
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
        super().__init__();self.links=[];self.href=None;self.parts=[];self.json_script=None

    def handle_starttag(self, tag, attrs):
        attrs=dict(attrs)
        if tag == 'a':self.href=attrs.get('href');self.parts=[]
        if tag == 'img' and self.href and attrs.get('alt'):self.parts.append(attrs['alt'])
        if tag == 'script' and attrs.get('type') in {'application/json','application/ld+json'}:self.json_script=[]
        for key,value in attrs.items():
            if key.startswith('data-') and value and value.lstrip().startswith(('{','[')):
                try:self.structured_links(json.loads(value))
                except ValueError:pass

    def handle_data(self, text):
        if self.href is not None:self.parts.append(text)
        if self.json_script is not None:self.json_script.append(text)

    def handle_endtag(self, tag):
        if tag == 'script' and self.json_script is not None:
            try:self.structured_links(json.loads(''.join(self.json_script)))
            except ValueError:pass
            self.json_script=None
        if tag == 'a' and self.href is not None:
            self.links.append((self.href,' '.join(' '.join(self.parts).split())))
            self.href=None;self.parts=[]

    def structured_links(self, value):
        if isinstance(value,list):
            for item in value:self.structured_links(item)
        elif isinstance(value,dict):
            url=next((value[k] for k in ['url','link','href'] if isinstance(value.get(k),str)),None)
            title=value.get('headline',value.get('title',value.get('name')))
            if url and isinstance(title,str):
                date=value.get('datePublished',value.get('date',''))
                self.links.append((url,re.sub('<[^>]+>',' ',title)+' '+str(date)))
            for item in value.values():
                if isinstance(item,(dict,list)):self.structured_links(item)


def hinted_date(text: str) -> str | None:
    match=re.search(r'\b(20\d{2})[-/.](\d{1,2})[-/.](\d{1,2})(?!\d)',text)
    if not match:match=re.search(r'(20\d{2})年(\d{1,2})月(\d{1,2})日|(?<!\d)(20\d{2})(\d{2})(\d{2})(?!\d)',text)
    if match:
        try:return datetime(*(int(x) for x in match.groups() if x is not None)).date().isoformat()
        except ValueError:return None
    for pattern in ['%b %d, %Y','%B %d, %Y','%b %d %Y']:
        match=re.search(r'\b[A-Z][a-z]+\s+\d{1,2},?\s+20\d{2}\b',text)
        if match:
            try:return datetime.strptime(match.group(),pattern).date().isoformat()
            except ValueError:pass
    return None


def recent_links(content: str, base: str, window: dict, limit: int, *, link_pattern: str | None = None,
                 kind: str = 'auto', stats: dict | None = None) -> list[dict]:
    candidates=[]
    try:
        root=ET.fromstring(content)
        if root.tag.rsplit('}',1)[-1].lower() not in {'rss','feed','rdf'}:
            raise ET.ParseError('Not a feed')
        for item in [*root.findall('.//{*}item'),*root.findall('.//{*}entry')]:
            title=item.findtext('{*}title','')
            links=item.findall('{*}link')
            link=next((v for v in links if v.get('rel','alternate')=='alternate'),None)
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
        if kind == 'rss' or re.search(r'<(?:rss|feed|rdf:RDF)\b',content[:1000],re.I):
            raise ValueError('Invalid or truncated publisher feed')
        parser=IndexParser();parser.feed(content)
        for url,title in parser.links:
            date=hinted_date(title) or hinted_date(url)
            article_link=re.search(link_pattern,urljoin(base,url)) if link_pattern else re.search(r'/(blog|news|articles?|press)/[^/#?]+|/20\d{2}/\d{1,2}/[^/#?]+|/\d+/\d+/\d+\.htm',url)
            if (date or (len(title)>=4 and article_link)) and (not link_pattern or article_link):
                candidates.append((url,title,date,title,None))
        if kind == 'index' and not candidates:raise ValueError('No recognizable article links in publisher index')
    if stats is not None:stats['entries']=len(candidates)
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
    indexes=[s for s in cfg.get('sources',[]) if s['section_id'] in ids and s.get('enabled',True)]
    indexes += [{**source,'section_id':section['id']} for section in settings.sections for source in section.get('sources',[]) if source.get('enabled',True)]
    indexes=list({(s['section_id'],s['url']):s for s in indexes}.values())
    by_url={}
    for item in indexes:by_url.setdefault(item['url'],[]).append(item)
    budget=min(128,cfg.get('max_indexes_per_edition',32))
    if len(by_url)>budget:raise ValueError('Configured sources exceed discovery budget; no source may be silently skipped')
    limit=min(30,cfg.get('max_links_per_index',15))
    def read(items):
        url=items[0]['url'];page=None;error=None
        try:
            page=fetch_page(settings,url,work,max_chars=2000000)
        except (ValueError,KeyError,OSError,subprocess.SubprocessError) as exc:
            error=type(exc).__name__
        results=[]
        for item in items:
            result={'section_id':item['section_id'],'query':'publisher index','index_url':url,
                    'required':item.get('required',False),'engines_ok':0,'sources':[],'index_status':error}
            if page is not None:
                try:
                    if item.get('kind')=='page':
                        text=page_text(page['content'],page['kind'])
                        if len(text)<100:raise ValueError('Page has no usable content')
                        links=[{'url':url,'title':item.get('name',url),'snippet':text[:800],
                                'published_at':publication_time(page['content']),'discovery_index':url}]
                    else:links=recent_links(page['content'],url,window,limit,kind=item.get('kind','index'),
                                            link_pattern=item.get('link_pattern'))
                    result.update(engines_ok=1,sources=links,index_status='links' if links else 'no_recent_links')
                except (ValueError,KeyError) as exc:result['index_status']=type(exc).__name__
            results.append(result)
        return results
    with ThreadPoolExecutor(max_workers=4) as pool:results=[r for rows in pool.map(read,by_url.values()) for r in rows]
    failed=[r['index_url'] for r in results if r.get('required') and r['index_status'] not in {'links','no_recent_links'}]
    if failed:raise ValueError('Required sources could not be read: '+', '.join(failed))
    good=[r for r in results if r['engines_ok']]
    return results,{'discovery_indexes':len(indexes),'discovery_requests':len(by_url),'discovery_indexes_ok':len(good),
                 'discovery_links':sum(len(r['sources']) for r in good),
                 'publisher_indexes':[{'section_id':r['section_id'],'url':r['index_url'],
                                       'status':r['index_status'],'links':len(r['sources'])} for r in results]}
