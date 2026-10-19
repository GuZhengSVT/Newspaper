import test from 'node:test';
import assert from 'node:assert/strict';
import { publicAddress } from '../scripts/fetch.mjs';
import { searchQuery } from '../scripts/search.mjs';

test('reject local and metadata IP addresses',()=>{
  for(const ip of ['127.0.0.1','169.254.169.254','10.0.0.1','192.168.1.1','172.31.0.1','::1','::ffff:127.0.0.1','fe80::1','fd00::1']) assert.equal(publicAddress(ip),false,ip);
});
test('accept ordinary public addresses',()=>{
  assert.equal(publicAddress('93.184.216.34'),true);
  assert.equal(publicAddress('2606:2800:220:1:248:1893:25c8:1946'),true);
});

test('rate-limited engines cool down while other DSH engines keep supplying candidates',async()=>{
  const state=new Map();let exaCalls=0;
  const providers={
    exa:async()=>{exaCalls++;throw Error('Exa MCP error (HTTP 429)');},
    anysearch:async()=>({sources:[{url:'https://example.test/new',title:'Synthetic release',snippet:'New announcement'}]}),
  };
  for(let i=0;i<2;i++){
    const result=await searchQuery({section_id:'fixture',query:'release'},['exa','anysearch'],100,state,providers);
    assert.equal(result.sources.length,1);
    assert.ok(result.engine_status.some(e=>e.engine==='exa'&&e.status==='rate_limited'));
    assert.ok(result.engine_status.some(e=>e.engine==='anysearch'&&e.status==='results'));
  }
  assert.equal(exaCalls,1);
  const concurrentState=new Map();
  const concurrent=await Promise.all([0,1].map(()=>searchQuery(
    {section_id:'fixture',query:'release'},['exa','anysearch'],100,concurrentState,providers)));
  assert.ok(concurrent.every(result=>result.engine_status.length===2));
});
