"""AI-planned searches through DSH plugin exports, without a growing agent transcript."""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlsplit

from .config import ROOT
from .evidence import validate_selection, context
from .model import parse_json
from .research import source_url, fetch_source
from .discovery import discover


class SearchPlanError(ValueError):
    def __init__(self, problems):
        super().__init__('Invalid search plan or missing section: '+', '.join(p['code'] for p in problems))
        self.problems=problems


def plan_problems(queries, sections, mandatory, covered, limit, gap_limit):
    """Contract diagnostics contain counts and IDs, never model response text."""
    if not isinstance(queries,list):return [{'code':'queries_not_list'}]
    problems=[];counts={sid:0 for sid in sections};gap_count=0
    if len(queries)>limit:problems.append({'code':'total_limit','count':len(queries),'limit':limit})
    for index,query in enumerate(queries):
        if not isinstance(query,dict):
            problems.append({'code':'invalid_entry','index':index});continue
        sid=query.get('section_id');text=query.get('query')
        if not isinstance(sid,str) or sid not in sections:
            problems.append({'code':'unknown_section','index':index});continue
        if not isinstance(text,str) or not text.strip() or len(text)>220:
            problems.append({'code':'invalid_query_text','index':index});continue
        counts[sid]+=1
        if sid in covered and not sections[sid].get('search',{}).get('queries'):gap_count+=1
    missing=sorted(sid for sid in mandatory if not counts[sid])
    if missing:problems.append({'code':'missing_sections','section_ids':missing})
    for sid,section in sections.items():
        cap=max(section.get('search',{}).get('query_count',2),len(section.get('search',{}).get('queries',[])))
        if counts[sid]>cap:problems.append({'code':'section_limit','section_id':sid,'count':counts[sid],'limit':cap})
    if gap_count>gap_limit:problems.append({'code':'gap_limit','count':gap_count,'limit':gap_limit})
    return problems


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
    sections_by_id={s['id']:s for s in settings.sections}
    names={s['id']:s['name'] for s in settings.sections}
    indexes,discovery_metrics=discover(settings,window,work)
    if diagnostics is not None:diagnostics.update(discovery_metrics)
    # Only a source that yielded recent links counts as coverage. A feed that was read but
    # had nothing inside the window still leaves a gap the planner must fill by searching.
    covered={r['section_id'] for r in indexes if r.get('index_status')=='links' and r.get('sources')}
    cfg=settings.values.get('discovery',{})
    gap_limit=cfg.get('max_gap_queries',3)
    mandatory={s['id'] for s in settings.sections if s['id'] not in covered and not s.get('search',{}).get('queries')}
    fixed_count=sum(len(s.get('search',{}).get('queries',[])) for s in settings.sections)
    limit=min(40,fixed_count+sum(s.get('search',{}).get('query_count',2) for s in settings.sections if s['id'] in mandatory)+gap_limit)
    queries=[];planned=[]
    for section in settings.sections:
        fixed=section.get('search',{}).get('queries',[])
        if fixed:
            substitutions={'date':window['date'],'window_start':window['window_start'][:10], 'window_end':window['window_end'][:10]}
            queries.extend({'section_id':section['id'],'query':q.format(**substitutions)} for q in fixed)
        else:planned.append({**{k:v for k,v in section.items() if k!='sources'},
                            'search':{**section.get('search',{}),'query_count':section.get('search',{}).get('query_count',2)}})
    if planned and (mandatory or gap_limit):
        coverage=[]
        for section in planned:
            rows=[r for r in indexes if r['section_id']==section['id']]
            titles=list(dict.fromkeys(r['sources'][i].get('title','')[:180]
                for i in range(max((len(r['sources']) for r in rows),default=0)) for r in rows if i<len(r['sources'])))
            coverage.append({'section_id':section['id'],'read_sources':sum(r.get('engines_ok',0)>0 for r in rows),
                'source_urls':[r.get('index_url','') for r in rows if r.get('engines_ok')],
                'failed_sources':[r.get('index_url','') for r in rows if not r.get('engines_ok')],
                'recent_links':sum(len(r['sources']) for r in rows),'titles':titles[:8]})
        query_limit=min(max(0,limit-len(queries)),sum(s['search']['query_count'] for s in planned))
        payload={'window':window,'sections':planned,'query_limit':query_limit,
            'strategy':'source_first','source_coverage':coverage,'mandatory_section_ids':sorted(mandatory),
            'max_gap_queries':min(gap_limit,query_limit),
            'section_query_limits':{s['id']:s['search']['query_count'] for s in planned}}
        for attempt in range(2):
            plan=client.request('plan',payload)
            proposed=plan.get('queries')
            errors=plan_problems(queries+proposed if isinstance(proposed,list) else proposed,
                                 sections_by_id,mandatory,covered,limit,gap_limit)
            if diagnostics is not None:
                diagnostics.setdefault('search_plan_checks',[]).append({'attempt':attempt+1,'problems':errors})
            if not errors:
                queries.extend(proposed);break
            payload={**payload,'validation_errors':errors}
        else:raise SearchPlanError(errors)
    errors=plan_problems(queries,sections_by_id,mandatory,covered,limit,gap_limit)
    if errors:raise SearchPlanError(errors)
    search_results=search(settings,queries,work,window) if queries else []
    results=indexes+search_results
    # Round-robin across publishers before applying a per-section model-input cap.
    # All configured indexes were read; unbounded feeds do not inflate model input.
    preview_cap=cfg.get('max_previews_per_section',24)
    allowed={sid:set() for sid in section_ids}
    for position in range(max((len(r['sources']) for r in results),default=0)):
        for result in results:
            sid=result['section_id']
            if position>=len(result['sources']) or len(allowed[sid])>=preview_cap:continue
            item=result['sources'][position]
            if any(word.casefold() in (item.get('title','')+' '+item.get('snippet','')).casefold()
                   for word in sections_by_id[sid].get('filter',{}).get('exclude_keywords',[])):continue
            try:allowed[sid].add(source_url(item['url']))
            except ValueError:continue
    previews={}
    url_ids={}
    all_urls=set()
    coverage=[]
    for result in results:
        if not result['engines_ok']:
            coverage.append(names[result['section_id']]+'的部分资料检索未完成，本期覆盖受限。')
        for item in result['sources']:
            try:url=source_url(item['url'])
            except ValueError:continue
            all_urls.add(url)
            if url not in allowed[result['section_id']]:continue
            sid=url_ids.setdefault(url,'s'+str(len(url_ids)+1))
            preview=previews.setdefault(sid,{'id':sid,'url':url,'section_ids':[]})
            if result['section_id'] not in preview['section_ids']:preview['section_ids'].append(result['section_id'])
            # A feed entry carries publisher provenance even if search found the URL first.
            if not preview.get('title') or item.get('discovery_index'):
                preview.update({k:v for k,v in item.items() if k not in {'snippet','url'}})
                preview['content']=item.get('snippet','')[:350]
    if not previews:raise ValueError('All search results empty; cannot claim a no-news edition')
    candidate_limit=settings.values.get('search',{}).get('max_candidates_per_edition',12)
    selection=client.request('select',{'window':window,'sections':[{k:v for k,v in s.items() if k!='sources'} for s in settings.sections],'sources':list(previews.values()),
        'material_kind':'search previews; facts and dates MUST be verified against fetched original pages',
        'candidate_limit':candidate_limit,'max_sources_per_candidate':3})
    candidates=validate_selection(selection,settings,list(previews.values()),candidate_limit)
    cap=settings.values.get('search',{}).get('max_fetches_per_edition',24)
    # Give every candidate a first original before spending the budget on corroboration.
    selected=list(dict.fromkeys(c['source_ids'][i] for i in range(3) for c in candidates
                                if i<len(c['source_ids'])))[:cap]
    empty_selection_probe=not candidates
    if empty_selection_probe:
        # An empty preview selection is not a fetch failure. Check real articles from
        # distinct publishers before accepting an empty issue, within the existing budget.
        publishers={}
        for sid,preview in previews.items():publishers.setdefault(urlsplit(preview['url']).hostname,sid)
        selected=list(publishers.values())[:min(3,cap)]
    sources=[{**s,'id':url_ids[source_url(s['url'])]} for s in fetch(settings,[previews[sid] for sid in selected],work,cache)]
    available={s['id'] for s in sources}
    recovery=[]
    remaining_gap=max(0,gap_limit-sum(q['section_id'] in covered for q in queries))
    for candidate in candidates:
        if any(sid in available for sid in candidate['source_ids']):continue
        if not remaining_gap or len(selected)>=cap:break
        remaining_gap-=1
        query=' '.join(candidate['entities']+[str(candidate.get('focus') or candidate['event_key'])])[:220]
        attempt={'section_id':candidate['section_id'],'event_key':candidate['event_key'],'fetched_sources':0}
        recovery.append(attempt)
        try:
            alternatives=search(settings,[{'section_id':candidate['section_id'],'query':query}],work,window)
            extra=[]
            for result in alternatives:
                for item in result['sources']:
                    try:url=source_url(item['url'])
                    except (KeyError,ValueError):continue
                    sid=url_ids.get(url)
                    if sid in selected or sid in extra:continue
                    if sid is None:
                        sid='s'+str(len(url_ids)+1);url_ids[url]=sid;all_urls.add(url)
                        previews[sid]={**item,'id':sid,'url':url,'section_ids':[candidate['section_id']]}
                    extra.append(sid)
                    if len(extra)>=min(4,cap-len(selected)):break
                if len(extra)>=min(4,cap-len(selected)):break
            selected.extend(extra)
            recovered=[{**s,'id':url_ids[source_url(s['url'])]}
                       for s in fetch(settings,[previews[sid] for sid in extra],work,cache)]
            sources.extend(recovered);available.update(s['id'] for s in recovered)
            candidate['source_ids'] += [s['id'] for s in recovered]
            attempt['fetched_sources']=len(recovered)
        except (ValueError,subprocess.SubprocessError) as exc:attempt['error']=type(exc).__name__
    if empty_selection_probe and sources:
        selection=client.request('select',{'window':window,
            'sections':[{k:v for k,v in s.items() if k!='sources'} for s in settings.sections],
            'sources':[{**s,'content':context(s['content'],[])} for s in sources],
            'material_kind':'fetched originals; recheck the empty preview selection against actual article evidence',
            'candidate_limit':candidate_limit,'max_sources_per_candidate':3})
        candidates=validate_selection(selection,settings,sources,candidate_limit)
    kept=[]
    for candidate in candidates:
        candidate['source_ids']=[s for s in candidate['source_ids'] if s in available]
        if candidate['source_ids']:kept.append(candidate)
        else:coverage.append('部分候选原文无法取得，已放弃报道，不能据此判断该板块没有重要事件。')
    engine_status={}
    for result in results:
        for status in result.get('engine_status',[]):
            key=status['engine']+':'+status['status'];engine_status[key]=engine_status.get(key,0)+1
    metrics={'collection_strategy':'source_first','plugin_queries':len(queries)+len(recovery),
             'search_engines_ok':sum(r['engines_ok'] for r in search_results),
             'source_covered_sections':sorted(covered),'search_gap_queries':sum(q['section_id'] in covered for q in queries),
             'preview_urls_omitted':len(all_urls)-len(previews),
             'result_urls':len(previews),'fetched_sources':len(sources),'selected_candidates':len(candidates),
             'empty_selection_probe':empty_selection_probe,
             'source_recovery':recovery,
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
