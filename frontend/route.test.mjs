// Leaving a slow page must not let its loading placeholder (or error screen) cover the next page.
// app.js is a browser script, so this pulls navTicket out of it and replays the router's timer logic.
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import vm from 'node:vm';

const app = readFileSync(new URL('public/app.js', import.meta.url), 'utf8');
const helper = app.match(/function navTicket\(\) \{[\s\S]*?\r?\n\}\r?\nnavTicket\.latest = 0;/);
assert.ok(helper, 'navTicket not found in app.js');
const { navTicket } = vm.runInNewContext(`${helper[0]}\n({ navTicket })`);

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
console.log('route.test.mjs: ok');
