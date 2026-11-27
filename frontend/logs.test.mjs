// A failed first Logs load must not leave placeholder rows spinning; a failed later refresh keeps the entries shown.
// app.js is a browser script, so this runs its logsView with a tiny fake DOM.
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import vm from 'node:vm';

const app = readFileSync(new URL('public/app.js', import.meta.url), 'utf8');
const source = app.match(/function logsView\(projectId\) \{[\s\S]*?\r?\n\}\r?\n/);
assert.ok(source, 'logsView not found in app.js');

class Node {
  constructor(tag, props = {}, children = []) { Object.assign(this, { tag, attrs: {}, children: [] }, props); this.replaceChildren(...children); }
  replaceChildren(...children) { this.children = children.flat().filter((child) => child != null && child !== false); }
  setAttribute(key, value) { this.attrs[key] = value; }
  removeAttribute(key) { delete this.attrs[key]; }
  find(test) { return test(this) ? this : this.children.map((child) => child instanceof Node && child.find(test)).find(Boolean); }
}
const el = (tag, props, ...children) => new Node(tag, props, children);
let respond;
const context = {
  el, text: (tag, value, className) => el(tag, { className, textContent: value }), icon: () => null,
  skelRows: () => el('div', { className: 'skel-rows' }), select: () => { const input = el('select'); return [el('label', {}, input), input]; },
  action: (button, work) => () => work(), showToast: () => {}, logMessage: () => '', LEVEL_NAMES: {}, SOURCE_NAMES: {},
  api: () => respond(), URLSearchParams, document: { body: { contains: () => true } },
};
const logsView = vm.runInNewContext(`${source[0]}\nlogsView`, context);
const settle = () => new Promise((resolve) => setTimeout(resolve, 10));
const list = (view) => view.find((node) => node.className === 'log-list');
const placeholders = (node) => Boolean(node.find((child) => child.className === 'skel-rows'));
const alert = (node) => node.find((child) => child.role === 'alert');

respond = () => Promise.reject(new Error('Network down'));
const view = logsView(null);
await settle();
assert.equal(placeholders(list(view)), false, 'placeholder rows still shown after the first load failed');
assert.ok(alert(list(view)), 'no error shown after the first load failed');
assert.equal(list(view).attrs['aria-busy'], undefined);

respond = () => Promise.resolve({ entries: [{ at: 1, level: 'info', source: 'app', event: 'x', data: {} }], next: null });
await alert(list(view)).find((node) => node.tag === 'button').onclick();
assert.equal(alert(list(view)), undefined, 'Try again did not load the entries');
const shown = list(view).children.length;

respond = () => Promise.reject(new Error('Network down'));
const refresh = view.find((node) => node.tag === 'button' && node.children.includes('Refresh'));
await refresh.onclick().catch(() => {});
assert.equal(list(view).children.length, shown, 'a failed refresh removed the entries already shown');
assert.equal(alert(list(view)), undefined);

// Issue 31: answers arriving out of order. Each request waits until the test answers it.
const pending = [];
respond = () => new Promise((resolve) => pending.push(resolve));
const row = (level, at) => ({ at, level, source: 'app', event: level, data: {} });
const levels = (node) => node.children.filter((child) => child.tag === 'details').map((child) => child.className.split(' ')[1]);
const answer = async (index, entries, next = null) => { pending[index]({ entries, next }); await settle(); };

const filtered = logsView(null); // request 0: unfiltered
const level = filtered.find((node) => node.tag === 'select');
level.value = 'error';
level.onchange(); // request 1: errors only
await answer(0, [row('info', 1)]);
assert.equal(list(filtered).attrs['aria-busy'], 'true', 'the old answer cleared the busy state while the filtered request was still pending');
assert.deepEqual(levels(list(filtered)), [], 'the old unfiltered answer was drawn');
await answer(1, [row('error', 2)]);
assert.deepEqual(levels(list(filtered)), ['error']);
assert.equal(list(filtered).attrs['aria-busy'], undefined);

pending.length = 0;
const reversed = logsView(null);
const levelB = reversed.find((node) => node.tag === 'select');
levelB.value = 'error';
levelB.onchange();
await answer(1, [row('error', 2)]);
await answer(0, [row('info', 1)]);
assert.deepEqual(levels(list(reversed)), ['error'], 'a late unfiltered answer replaced the filtered rows');

// A filter change while "Load older" is in flight: the older page belongs to the old list and is dropped.
pending.length = 0;
const paged = logsView(null);
await answer(0, [row('info', 5)], 4);
const more = paged.find((node) => node.tag === 'button' && node.children.includes('Load older'));
more.onclick(); // request 1: older entries for the unfiltered list
const levelC = paged.find((node) => node.tag === 'select');
levelC.value = 'error';
levelC.onchange(); // request 2
await answer(2, [row('error', 3)], 2);
await answer(1, [row('info', 4)], 3);
assert.deepEqual(levels(list(paged)), ['error'], 'older unfiltered entries were appended to the error list');
assert.equal(more.hidden, false, 'the cursor from the error list was lost');
console.log('logs.test.mjs: ok');
