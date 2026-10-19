import test from 'node:test';
import assert from 'node:assert/strict';
import { publicAddress, checkAddress } from '../scripts/fetch.mjs';
import { searchQuery } from '../scripts/search.mjs';
import { configureProxy } from '../scripts/network.mjs';
import http from 'node:http';
import https from 'node:https';
import { getGlobalDispatcher, setGlobalDispatcher } from 'undici';

test('reject local and metadata IP addresses',()=>{
  for(const ip of ['127.0.0.1','169.254.169.254','10.0.0.1','192.168.1.1','172.31.0.1','::1','::ffff:127.0.0.1','fe80::1','fd00::1']) assert.equal(publicAddress(ip),false,ip);
});
test('accept ordinary public addresses',()=>{
  assert.equal(publicAddress('93.184.216.34'),true);
  assert.equal(publicAddress('2606:2800:220:1:248:1893:25c8:1946'),true);
});

test('proxy DNS fallback still rejects private literals and known private resolutions',async()=>{
  const missing=async()=>{throw Object.assign(Error('DNS unavailable'),{code:'EAI_AGAIN'});};
  await checkAddress('example.test',true,missing);
  await assert.rejects(checkAddress('example.test',false,missing));
  for(const host of ['127.0.0.1','localhost','service.localhost','::1']) await assert.rejects(checkAddress(host,true,missing));
  await assert.rejects(checkAddress('example.test',true,async()=>[{address:'10.0.0.1'}]));
});

test('configured egress proxy handles both Node HTTPS and fetch transports',async()=>{
  const saved={...process.env},dispatcher=getGlobalDispatcher(),httpAgent=http.globalAgent,httpsAgent=https.globalAgent;
  const destinations=[];
  const proxy=http.createServer();
  proxy.on('connect',(request,socket)=>{
    destinations.push(request.url);socket.end('HTTP/1.1 502 Test Proxy\r\nConnection: close\r\n\r\n');
  });
  await new Promise(resolve=>proxy.listen(0,'127.0.0.1',resolve));
  try {
    for(const key of ['https_proxy','http_proxy','HTTPS_PROXY','HTTP_PROXY','ALL_PROXY','all_proxy','NO_PROXY','no_proxy']) delete process.env[key];
    process.env.HTTPS_PROXY=`http://127.0.0.1:${proxy.address().port}`;
    assert.equal(configureProxy(),true);
    const status=await new Promise((resolve,reject)=>https.get('https://example.test/',response=>{response.resume();resolve(response.statusCode);}).on('error',reject));
    assert.equal(status,502);
    await assert.rejects(fetch('https://example.test/'));
    assert.deepEqual(destinations,['example.test:443','example.test:443']);
  } finally {
    http.globalAgent.destroy();http.globalAgent=httpAgent;https.globalAgent=httpsAgent;
    await getGlobalDispatcher().close();setGlobalDispatcher(dispatcher);
    for(const key of Object.keys(process.env)) if (!(key in saved)) delete process.env[key];
    Object.assign(process.env,saved);
    await new Promise(resolve=>proxy.close(resolve));
  }
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

test('exhausted anonymous quota stops repeated calls and preserves the working engine',async()=>{
  const state=new Map();let calls=0;
  const providers={anysearch:async()=>{calls++;throw Error('AnySearch API error (HTTP 402)');},
    keenable:async()=>({sources:[{url:'https://example.test/news',title:'Synthetic update'}]})};
  for(let i=0;i<2;i++){
    const result=await searchQuery({section_id:'fixture',query:'news'},['anysearch','keenable'],100,state,providers);
    assert.equal(result.sources.length,1);
    assert.ok(result.engine_status.some(e=>e.engine==='anysearch'&&e.status==='quota_exhausted'));
  }
  assert.equal(calls,1);
});
