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
  skelRows: () => el('div', { className: 'skel-rows' }), select: () => [el('label'), el('select')],
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
console.log('logs.test.mjs: ok');
