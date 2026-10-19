"""AI-planned searches through DSH plugin exports, without a growing agent transcript."""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .config import ROOT
from .evidence import validate_selection
from .model import parse_json
from .research import source_url, fetch_source


def search(settings, queries: list[dict], work: Path) -> list[dict]:
    work.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(['node',str(ROOT/'scripts/search.mjs')],
        input=json.dumps({'queries':queries,'engines':settings.values.get('search',{}).get('engines',['bing','exa'])}),
        text=True,capture_output=True,check=True,timeout=max(45,len(queries)*12+30),
        env={**os.environ,'RESEARCH_CACHE_DIR':str(work/'cache'),'NODE_OPTIONS':'--max-old-space-size=128'})
    return parse_json(result.stdout)['results']


def fetch(settings, candidates: list[dict], work: Path, cache: dict) -> list[dict]:
    by_url={source_url(s['url']):s for s in candidates}
    missing=[s for u,s in by_url.items() if u not in cache]
    with ThreadPoolExecutor(max_workers=2) as pool:
        values=list(pool.map(lambda s:fetch_source(settings,s,work),missing))
    for item,value in zip(missing,values):cache[source_url(item['url'])]=value
    # Collapse byte-identical bodies, but keep different sources even when they agree.
    unique={}
    for url in by_url:
        value=cache[url]
        if value:unique.setdefault(hashlib.sha256(value['content'].encode()).hexdigest(),value)
    return list(unique.values())


def research(settings, client, window: dict, work: Path, cache: dict) -> tuple[list,list,list,list,dict]:
    section_ids={s['id'] for s in settings.sections}
    names={s['id']:s['name'] for s in settings.sections}
    limit=min(40,len(section_ids)*2)
    plan=client.request('plan',{'window':window,'sections':settings.sections,'query_limit':limit})
    queries=plan['queries']
    if (not isinstance(queries,list) or len(queries)>limit or any(q.get('section_id') not in section_ids
        or not isinstance(q.get('query'),str) or not 1<=len(q['query'])<=220 for q in queries)
        or {q['section_id'] for q in queries}!=section_ids):
        raise ValueError('Invalid search plan or missing section')
    results=search(settings,queries,work)
    previews={}
    url_ids={}
    coverage=[]
    for result in results:
        if not result['engines_ok']:
            coverage.append(names[result['section_id']]+'的部分资料检索未完成，本期覆盖受限。')
        for item in result['sources']:
            try:url=source_url(item['url'])
            except ValueError:continue
            sid=url_ids.setdefault(url,'s'+str(len(url_ids)+1))
            previews.setdefault(sid,{'id':sid,**{k:v for k,v in item.items() if k!='snippet'},'url':url,'content':item.get('snippet','')})
    if not previews:raise ValueError('All search results empty; cannot claim a no-news edition')
    candidate_limit=settings.values.get('search',{}).get('max_candidates_per_edition',12)
    selection=client.request('select',{'window':window,'sections':settings.sections,'sources':list(previews.values()),
        'material_kind':'search previews; facts and dates MUST be verified against fetched original pages',
        'candidate_limit':candidate_limit,'max_sources_per_candidate':3})
    candidates=validate_selection(selection,settings,list(previews.values()),candidate_limit)
    selected={sid for c in candidates for sid in c['source_ids'][:3]}
    cap=settings.values.get('search',{}).get('max_fetches_per_edition',24)
    selected=list(dict.fromkeys(sid for c in candidates for sid in c['source_ids'][:3]))[:cap]
    sources=[{**s,'id':url_ids[source_url(s['url'])]} for s in fetch(settings,[previews[sid] for sid in selected],work,cache)]
    available={s['id'] for s in sources}
    kept=[]
    for candidate in candidates:
        candidate['source_ids']=[s for s in candidate['source_ids'] if s in available]
        if candidate['source_ids']:kept.append(candidate)
        else:coverage.append('部分候选原文无法取得，已放弃报道，不能据此判断该板块没有重要事件。')
    if candidates and not sources:raise ValueError('No selected originals accessible')
    metrics={'plugin_queries':len(queries),'search_engines_ok':sum(r['engines_ok'] for r in results),
             'result_urls':len(previews),'fetched_sources':len(sources)}
    return sources,kept,coverage,results,metrics


def supplement(settings, section: dict, query: str, work: Path, cache: dict) -> list[dict]:
    results=search(settings,[{'section_id':section['id'],'query':query[:220]}],work)
    candidates=[s for r in results for s in r['sources']][:4]
    return fetch(settings,candidates,work,cache)
