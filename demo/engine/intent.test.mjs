// The demo's request parser must pick the cloud the user names, for every cloud the dashboard can connect.
// Run: node demo/engine/intent.test.mjs (CI runs it). intent.js is an ES module without a .mjs name, so it is loaded as source.
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

const source = readFileSync(new URL('./intent.js', import.meta.url), 'utf8');
const { proposalFor } = await import(`data:text/javascript,${encodeURIComponent(source)}`);
const project = (clouds, active = 'aws') => ({ clouds: Object.fromEntries(['aws', ...clouds].map((cloud) => [cloud, {}])), active, previous: null, portability: 'auto' });

for (const [cloud, name] of [['digitalocean', 'DigitalOcean'], ['hetzner', 'Hetzner'], ['hostinger', 'Hostinger']]) {
  const connected = project([cloud]);
  const moved = proposalFor(`switch to ${cloud}`, connected);
  assert.equal(moved.action, 'switch', `switch to ${cloud}: ${JSON.stringify(moved)}`);
  assert.equal(moved.cloud, cloud);
  assert.equal(proposalFor(`destroy ${cloud}`, connected).cloud, cloud);
  assert.equal(proposalFor(`verify ${cloud}`, connected).cloud, cloud);
  assert.equal(proposalFor(`plan on ${cloud}`, connected).cloud, cloud);
  assert.equal(proposalFor(`switch to ${cloud}`, project([])).reply, `${name} is not connected yet.`);
}
assert.equal(proposalFor('switch to gcp', project(['gcp'])).cloud, 'gcp', 'the first two clouds still work');
console.log('demo intent: ok');
