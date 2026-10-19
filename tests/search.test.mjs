import test from 'node:test';
import assert from 'node:assert/strict';
import { publicAddress } from '../scripts/fetch.mjs';

test('reject local and metadata IP addresses',()=>{
  for(const ip of ['127.0.0.1','169.254.169.254','10.0.0.1','192.168.1.1','172.31.0.1','::1','::ffff:127.0.0.1','fe80::1','fd00::1']) assert.equal(publicAddress(ip),false,ip);
});
test('accept ordinary public addresses',()=>{
  assert.equal(publicAddress('93.184.216.34'),true);
  assert.equal(publicAddress('2606:2800:220:1:248:1893:25c8:1946'),true);
});
