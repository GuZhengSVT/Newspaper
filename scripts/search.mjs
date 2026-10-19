// Execute only the pinned DSH plugin's keyless engines. No LLM/tool loop or daemon.
import {searchBing, searchDdgHtml, searchExaMCP, searchAnysearch, searchKeenableMCP} from 'dsh-free-search';
import {configureProxy} from './network.mjs';

export async function searchQuery(item, engines, timeoutMs = 20000, state = new Map(), providers = null, timeRange = {days:2}) {
  // Bing HTML, AnySearch and anonymous Exa do not implement timeRange.
  // The planner supplies one dated query and originals determine eligibility.
  const options = {lang: /[\u3400-\u9fff]/.test(item.query) ? 'zh' : 'en'};
  const calls = providers || {bing:s => searchBing(item.query, 5, options, s),
    ddg:s => searchDdgHtml(item.query, 5, {...options,timeRange}, s), exa:s => searchExaMCP(item.query, 5, s),
    keenable:s => searchKeenableMCP(item.query, 5, timeRange, s),
    anysearch:s => searchAnysearch(item.query, 5, s)};
  const active = engines.filter(engine => !state.has(engine));
  const settled = await Promise.allSettled(active.map(engine => {
    if (!calls[engine]) throw Error('Unsupported search engine');
    return calls[engine](AbortSignal.timeout(timeoutMs));
  }));
  const sources = new Map(); let ok = 0;
  const engine_status = engines.filter(engine => !active.includes(engine)).map(engine => ({engine,status:state.get(engine)}));
  for (const [i,result] of settled.entries()) {
    const engine = active[i];
    if (result.status !== 'fulfilled') {
      const message = String(result.reason);
      const status = /429|rate.limit/i.test(message) ? 'rate_limited' : /HTTP 402/.test(message) ? 'quota_exhausted' : 'unavailable';
      if (status === 'rate_limited' || status === 'quota_exhausted') state.set(engine,status);
      engine_status.push({engine,status});
      continue;
    }
    ok++;
    engine_status.push({engine,status:result.value.sources?.length ? 'results' : 'empty'});
    for (const s of result.value.sources || []) {
      if (typeof s.url !== 'string' || !s.url.startsWith('https://')) continue;
      if (!sources.has(s.url)) sources.set(s.url,{url:s.url,title:String(s.title||'').slice(0,300),
        snippet:String(s.snippet||'').slice(0,800),published_at:s.publishedAt||null});
    }
  }
  return {section_id:item.section_id,query:item.query,engines_ok:ok,engine_status,sources:[...sources.values()]};
}

async function main() {
  configureProxy();
  let input='';for await (const chunk of process.stdin) input+=chunk;
  const {queries,engines=['bing','keenable','exa'],window_start}=JSON.parse(input);
  const timeRange=window_start ? {after:window_start.slice(0,10),days:Math.max(1,Math.ceil((Date.now()-Date.parse(window_start))/86400000))} : {days:2};
  if (!Number.isFinite(timeRange.days)) throw Error('Invalid search window');
  if (!Array.isArray(queries)||queries.length>40||!engines.length||engines.length>3||engines.some(e=>!['bing','ddg','exa','anysearch','keenable'].includes(e))) throw Error('Invalid search budget');
  const results=new Array(queries.length);let next=0;
  const state=new Map();
  await Promise.all([0,1].map(async()=>{while(next<queries.length){const i=next++;results[i]=await searchQuery(queries[i],engines,20000,state,null,timeRange);}}));
  process.stdout.write(JSON.stringify({results}));
}

if (process.argv[1] && import.meta.url === new URL(process.argv[1], 'file:').href) {
  main().catch(()=>{process.stderr.write('DSH plugin search failed\n');process.exitCode=1;});
}
