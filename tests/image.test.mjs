import test from 'node:test';
import assert from 'node:assert/strict';
import sharp from 'sharp';
import {imageQuality, verifyImage} from '../scripts/image.mjs';

async function detailed(width=1000,height=650) {
  const pixels=Buffer.alloc(width*height*3);
  for(let i=0;i<pixels.length;i++)pixels[i]=(i*73+(i>>4)*19)%256;
  return sharp(pixels,{raw:{width,height,channels:3}}).png().toBuffer();
}

test('accept a decodable detailed editorial image; reject thumbnails, blank and corrupt images',async()=>{
  assert.deepEqual(await imageQuality(await detailed()),{width:1000,height:650});
  await assert.rejects(imageQuality(await detailed(200,150)));
  const blank=await sharp({create:{width:1000,height:650,channels:3,background:'white'}}).png().toBuffer();
  await assert.rejects(imageQuality(blank));
  await assert.rejects(imageQuality(Buffer.from('not an image')));
});

test('validate each redirect before fetching images',async()=>{
  let calls=0;
  const request=async()=>{calls++;return new Response(null,{status:302,headers:{location:'https://127.0.0.1/private'}});};
  const resolve=async host=>{if(host==='127.0.0.1')throw Error('Private image');};
  await assert.rejects(verifyImage('https://example.test/image',request,resolve));
  assert.equal(calls,1);
  for(const url of ['http://example.test/image','https://user:pass@example.test/image','https://example.test:8080/image'])
    await assert.rejects(verifyImage(url,request,resolve));
});

test('reject HTML errors and bound streamed image downloads',async()=>{
  const resolve=async()=>{};
  await assert.rejects(verifyImage('https://example.test/image',async()=>new Response('<html>blocked</html>',{headers:{'content-type':'text/html'}}),resolve));
  await assert.rejects(verifyImage('https://example.test/image',async()=>new Response(Buffer.alloc(8*1024*1024+1),{headers:{'content-type':'image/png'}}),resolve));
});
