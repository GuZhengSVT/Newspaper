// Verify externally hosted editorial images; keep only metadata, never publish a local copy.
import { pathToFileURL } from 'node:url';
import sharp from 'sharp';
import { checkAddress } from './fetch.mjs';
import { configureProxy } from './network.mjs';

export async function imageQuality(bytes) {
  const image=sharp(bytes,{limitInputPixels:40000000,failOn:'warning'});
  const metadata=await image.metadata();
  const {width,height,format,pages=1}=metadata;
  if (!['jpeg','png','webp'].includes(format) || pages!==1 ||
      Math.max(width,height)<800 || Math.min(width,height)<450 ||
      Math.max(width,height)/Math.min(width,height)>3 || width+height>10000) throw Error('Unsuitable image dimensions or format');
  // Decode as well as reading headers: malformed/truncated files must not pass.
  const {data,info}=await image.resize({width:400,height:400,fit:'inside',withoutEnlargement:true}).removeAlpha().greyscale().raw().toBuffer({resolveWithObject:true});
  let energy=0;
  for(let y=1;y<info.height-1;y++) for(let x=1;x<info.width-1;x++) {
    const i=y*info.width+x;
    energy+=(4*data[i]-data[i-1]-data[i+1]-data[i-info.width]-data[i+info.width])**2;
  }
  // Reject blank/strongly blurred placeholders conservatively; uncertain images are optional.
  if (energy/((info.width-2)*(info.height-2))<8) throw Error('Image lacks usable detail');
  return {width,height};
}

export async function verifyImage(url, request=fetch, resolveAddress=checkAddress) {
  const proxied=configureProxy();
  const signal=AbortSignal.timeout(15000);
  for(let redirects=0;redirects<=3;redirects++) {
    const parsed=new URL(url);
    if(parsed.protocol!=='https:' || parsed.username || parsed.password || parsed.port && parsed.port!=='443') throw Error('Invalid image URL');
    await resolveAddress(parsed.hostname.replace(/^\[|\]$/g,''),proxied);
    const response=await request(parsed.href,{redirect:'manual',signal});
    if([301,302,303,307,308].includes(response.status)) {
      await response.body?.cancel();
      const location=response.headers.get('location');
      if(!location) throw Error('Missing image redirect');
      url=new URL(location,parsed).href;continue;
    }
    if(!response.ok || !/^image\/(jpeg|png|webp)(?:;|$)/i.test(response.headers.get('content-type')||'')) {
      await response.body?.cancel();throw Error('Invalid image response');
    }
    const chunks=[];reader=response.body.getReader();let size=0;
    try {
      while(true) {
        const {value,done}=await reader.read();if(done)break;
        size+=value.length;
        if(size>8*1024*1024)throw Error('Image exceeds budget');
        chunks.push(value);
      }
    } finally {await reader.cancel();}
    return {url:parsed.href,...await imageQuality(Buffer.concat(chunks))};
  }
  throw Error('Too many image redirects');
}

if(process.argv[1] && import.meta.url===pathToFileURL(process.argv[1]).href) {
  let input='';for await(const chunk of process.stdin)input+=chunk;
  try {process.stdout.write(JSON.stringify(await verifyImage(JSON.parse(input).url)));}
  catch {process.stderr.write('Optional image rejected\n');process.exitCode=1;}
}
