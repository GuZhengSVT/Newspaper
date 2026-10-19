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
from .discovery import discover


def search(settings, queries: list[dict], work: Path, window: dict | None = None) -> list[dict]:
    work.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(['node',str(ROOT/'scripts/search.mjs')],
        input=json.dumps({'window_start':window.get('window_start') if window else None,'queries':queries,'engines':settings.values.get('search',{}).get('engines',['bing','keenable','exa'])}),
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


def research(settings, client, window: dict, work: Path, cache: dict, diagnostics: dict | None = None) -> tuple[list,list,list,list,dict]:
    section_ids={s['id'] for s in settings.sections}
    names={s['id']:s['name'] for s in settings.sections}
    limit=min(40,sum(max(s.get('search',{}).get('query_count',2),len(s.get('search',{}).get('queries',[]))) for s in settings.sections))
    queries=[];planned=[]
    for section in settings.sections:
        fixed=section.get('search',{}).get('queries',[])
        if fixed:
            substitutions={'date':window['date'],'window_start':window['window_start'][:10], 'window_end':window['window_end'][:10]}
            queries.extend({'section_id':section['id'],'query':q.format(**substitutions)} for q in fixed)
        else:planned.append(section)
    if planned:
        plan=client.request('plan',{'window':window,'sections':planned,'query_limit':max(1,limit-len(queries))})
        queries.extend(plan['queries'])
    if (not isinstance(queries,list) or len(queries)>limit or any(q.get('section_id') not in section_ids
        or not isinstance(q.get('query'),str) or not 1<=len(q['query'])<=220 for q in queries)
        or {q['section_id'] for q in queries}!=section_ids):
        raise ValueError('Invalid search plan or missing section')
    results=search(settings,queries,work,window)
    indexes,discovery_metrics=discover(settings,window,work)
    results.extend(indexes)
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
            preview=previews.setdefault(sid,{'id':sid,'url':url,'section_ids':[]})
            if result['section_id'] not in preview['section_ids']:preview['section_ids'].append(result['section_id'])
            # A feed entry carries publisher provenance even if search found the URL first.
            if not preview.get('title') or item.get('discovery_index'):
                preview.update({k:v for k,v in item.items() if k not in {'snippet','url'}})
                preview['content']=item.get('snippet','')
    if not previews:raise ValueError('All search results empty; cannot claim a no-news edition')
    candidate_limit=settings.values.get('search',{}).get('max_candidates_per_edition',12)
    selection=client.request('select',{'window':window,'sections':settings.sections,'sources':list(previews.values()),
        'material_kind':'search previews; facts and dates MUST be verified against fetched original pages',
        'candidate_limit':candidate_limit,'max_sources_per_candidate':3})
    candidates=validate_selection(selection,settings,list(previews.values()),candidate_limit)
    cap=settings.values.get('search',{}).get('max_fetches_per_edition',24)
    # Give every candidate a first original before spending the budget on corroboration.
    selected=list(dict.fromkeys(c['source_ids'][i] for i in range(3) for c in candidates
                                if i<len(c['source_ids'])))[:cap]
    sources=[{**s,'id':url_ids[source_url(s['url'])]} for s in fetch(settings,[previews[sid] for sid in selected],work,cache)]
    available={s['id'] for s in sources}
    kept=[]
    for candidate in candidates:
        candidate['source_ids']=[s for s in candidate['source_ids'] if s in available]
        if candidate['source_ids']:kept.append(candidate)
        else:coverage.append('部分候选原文无法取得，已放弃报道，不能据此判断该板块没有重要事件。')
    engine_status={}
    for result in results:
        for status in result.get('engine_status',[]):
            key=status['engine']+':'+status['status'];engine_status[key]=engine_status.get(key,0)+1
    metrics={'plugin_queries':len(queries),'search_engines_ok':sum(r['engines_ok'] for r in results),
             'result_urls':len(previews),'fetched_sources':len(sources),'selected_candidates':len(candidates),
             'candidates_with_originals':len(kept),'engine_status':engine_status,**discovery_metrics,
             'queries':queries,
             'candidate_fetches':[{'event_key':c['event_key'],'section_id':c['section_id'],
                                  'available_sources':len(c['source_ids'])} for c in candidates],
             'fetch_results':[{'url':previews[sid]['url'],'ok':sid in available,
                               'section_ids':previews[sid]['section_ids']} for sid in selected],
             'sections':{sid:{'result_urls':sum(sid in p['section_ids'] for p in previews.values()),
                              'selected_candidates':sum(c['section_id']==sid for c in candidates),
                              'candidates_with_originals':sum(c['section_id']==sid for c in kept)}
                         for sid in sorted(section_ids)}}
    if diagnostics is not None:diagnostics.update(metrics)
    if not sources:raise ValueError('No originals verified; refusing a no-news success')
    return sources,kept,coverage,results,metrics


def supplement(settings, section: dict, query: str, work: Path, cache: dict, window: dict | None = None) -> list[dict]:
    results=search(settings,[{'section_id':section['id'],'query':query[:220]}],work,window)
    candidates=[s for r in results for s in r['sources']][:4]
    return fetch(settings,candidates,work,cache)
