// Leaving a slow page must not let its loading placeholder (or error screen) cover the next page.
// app.js is a browser script, so this pulls navTicket out of it and replays the router's timer logic.
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import vm from 'node:vm';

const app = readFileSync(new URL('public/app.js', import.meta.url), 'utf8');
const helper = app.match(/function navTicket\(\) \{[\s\S]*?\r?\n\}\r?\nnavTicket\.latest = 0;[\s\S]*?function navLive\(\) \{[\s\S]*?\r?\n\}/);
assert.ok(helper, 'navTicket and navLive not found in app.js');
const { navTicket, navLive } = vm.runInNewContext(`${helper[0]}\n({ navTicket, navLive })`);

const drawn = [];
const timers = [];
// Same shape as route(): take a ticket, then draw the placeholder later only if still current.
const navigate = (page) => {
  const current = navTicket();
  timers.push(() => { if (current()) drawn.push(page); });
  return current;
};

const accounts = navigate('accounts'); // slow request still pending
const analyze = navigate('analyze'); // user moves on before 180 ms
timers.forEach((fire) => fire());
assert.deepEqual(drawn, ['analyze'], 'the stale Accounts timer drew over Analyze');
assert.equal(accounts(), false, 'a late Accounts failure would replace Analyze with its error screen');
assert.equal(analyze(), true);

// Issue 30: a renderer whose response arrives after the user moved on must not draw. Same shape as renderAccounts:
// it starts inside route(), awaits, then commits only if no navigation happened since.
const screen = [];
const renderer = async (page, response) => {
  const onPage = navLive();
  await response;
  if (onPage()) screen.push(page);
};
let release;
navTicket();
const slowAccounts = renderer('accounts', new Promise((resolve) => { release = resolve; }));
navTicket();
await renderer('analyze', Promise.resolve());
release();
await slowAccounts;
assert.deepEqual(screen, ['analyze'], 'the late Accounts response replaced Analyze');

// Every page renderer that waits for the network checks navLive before drawing, so a new one cannot forget.
const renderers = [...app.matchAll(/^async function (render\w+)\([^)]*\) \{([\s\S]*?)\r?\n\}/gm)];
assert.ok(renderers.length >= 8);
for (const [, name, body] of renderers) {
  // Only waits before the page is drawn matter; a poll that later updates a card in place writes to a detached node.
  // Top-level statements are indented two spaces; awaits inside nested helpers (the overview poll) are deeper.
  if (!/^ {2}\S.*await (api\(|Promise\.all)/m.test(body.split('setPage(')[0])) continue;
  assert.match(body, /const onPage = navLive\(\);[\s\S]*await[\s\S]*if \(!onPage\(\)\) return;/, `${name} draws without checking the user is still on its page`);
}
console.log('route.test.mjs: ok');
