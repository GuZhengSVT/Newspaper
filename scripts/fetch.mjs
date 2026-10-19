// Use the installed DSH plugin's original fetch provider, never a second scraper.
import { pathToFileURL } from 'node:url';
import { createRequire } from 'node:module';
import { dirname, join } from 'node:path';
import { lookup } from 'node:dns/promises';
import { isIP } from 'node:net';

export function publicAddress(address) {
  if (isIP(address) === 4) {
    const [a,b] = address.split('.').map(Number);
    return !(a === 0 || a === 10 || a === 127 || a === 169 && b === 254 || a === 172 && b >= 16 && b <= 31 || a === 192 && b === 168 || a === 100 && b >= 64 && b <= 127 || a >= 224);
  }
  const value = address.toLowerCase();
  return isIP(value) === 6 && !value.startsWith('::') && !value.startsWith('fc') && !value.startsWith('fd') && !/^fe[89ab]/.test(value);
}

async function main() {
  let input = '';
  for await (const chunk of process.stdin) input += chunk;
  const {url, max_chars = 200000} = JSON.parse(input);
  if (!Number.isInteger(max_chars) || max_chars < 1 || max_chars > 2000000) throw Error('Invalid page budget');
  const parsed = new URL(url);
  if (parsed.protocol !== 'https:' || parsed.username || parsed.password || parsed.port && parsed.port !== '443') throw Error('Invalid source URL');
  const addresses = await lookup(parsed.hostname.replace(/^\[|\]$/g, ''), {all:true});
  if (!addresses.length || addresses.some(a => !publicAddress(a.address))) throw Error('Private source address');
  const require = createRequire(import.meta.url);
  const entry = require.resolve('dsh-free-search');
  const {createFetchProvider} = await import(pathToFileURL(join(dirname(entry), 'fetch_provider.js')));
  const provider = createFetchProvider({resolveApiKey: async name => process.env[name] || undefined, logger:{warn(){}}});
  const result = await provider.fetch({url}, AbortSignal.timeout(30000));
  if (result.statusCode >= 400) throw Error('Source fetch failed');
  const content = result.body.content;
  // Never silently turn a truncated XML feed into an apparently empty HTML index.
  if (content.length > max_chars && /<(?:(?:\w+:)?rss|feed|rdf:RDF)\b/i.test(content.slice(0,1000))) throw Error('Feed exceeds page budget');
  process.stdout.write(JSON.stringify({kind:result.body.kind, content:content.slice(0,max_chars)}));
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  main().catch(() => {process.stderr.write('DSH source fetch failed\n'); process.exitCode=1;});
}
