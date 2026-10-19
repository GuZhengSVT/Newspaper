// Execute only the pinned DSH plugin's keyless engines. No LLM/tool loop or daemon.
import {searchBing, searchDdgHtml, searchExaMCP} from 'dsh-free-search';

export async function searchQuery(item, engines, timeoutMs = 20000) {
  const options = {lang: /[\u3400-\u9fff]/.test(item.query) ? 'zh' : 'en', timeRange:{days:2}};
  const calls = {bing:s => searchBing(item.query, 5, options, s),
    ddg:s => searchDdgHtml(item.query, 5, options, s), exa:s => searchExaMCP(item.query, 5, s)};
  const settled = await Promise.allSettled(engines.map(engine => {
    if (!calls[engine]) throw Error('Unsupported search engine');
    return calls[engine](AbortSignal.timeout(timeoutMs));
  }));
  const sources = new Map(); let ok = 0;
  for (const result of settled) {
    if (result.status !== 'fulfilled') continue;
    ok++;
    for (const s of result.value.sources || []) {
      if (typeof s.url !== 'string' || !s.url.startsWith('https://')) continue;
      if (!sources.has(s.url)) sources.set(s.url,{url:s.url,title:String(s.title||'').slice(0,300),
        snippet:String(s.snippet||'').slice(0,800),published_at:s.publishedAt||null});
    }
  }
  return {section_id:item.section_id,query:item.query,engines_ok:ok,sources:[...sources.values()]};
}

async function main() {
  let input='';for await (const chunk of process.stdin) input+=chunk;
  const {queries,engines=['bing','exa']}=JSON.parse(input);
  if (!Array.isArray(queries)||queries.length>40||!engines.length||engines.length>3||engines.some(e=>!['bing','ddg','exa'].includes(e))) throw Error('Invalid search budget');
  const results=new Array(queries.length);let next=0;
  await Promise.all([0,1].map(async()=>{while(next<queries.length){const i=next++;results[i]=await searchQuery(queries[i],engines);}}));
  process.stdout.write(JSON.stringify({results}));
}

if (process.argv[1] && import.meta.url === new URL(process.argv[1], 'file:').href) {
  main().catch(()=>{process.stderr.write('DSH plugin search failed\n');process.exitCode=1;});
}
