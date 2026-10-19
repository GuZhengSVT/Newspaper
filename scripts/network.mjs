// Both transports are used by the pinned search/fetch plugin.
import http from 'node:http';
import https from 'node:https';
import { ProxyAgent } from 'proxy-agent';
import { EnvHttpProxyAgent, setGlobalDispatcher } from 'undici';

export function configureProxy() {
  if (!['https_proxy','HTTPS_PROXY','http_proxy','HTTP_PROXY'].some(key => process.env[key])) return false;
  const agent = new ProxyAgent();
  http.globalAgent = agent;
  https.globalAgent = agent;
  setGlobalDispatcher(new EnvHttpProxyAgent());
  return true;
}
