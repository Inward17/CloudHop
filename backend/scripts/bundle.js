// Packs templates/ into a JS module, because Workers have no filesystem at runtime.
import { existsSync, readdirSync, readFileSync, writeFileSync } from 'node:fs';

const dir = new URL('../templates/', import.meta.url);
const out = new URL('../src/templates.gen.js', import.meta.url);
const templates = Object.fromEntries(readdirSync(dir).map((name) => [name, readFileSync(new URL(name, dir), 'utf8').replaceAll('\r\n', '\n')]));
const content = `export const TEMPLATES = ${JSON.stringify(templates, null, 2)};\n`;
// Wrangler watches src/, so rewriting an unchanged file would restart the dev server forever.
if (!existsSync(out) || readFileSync(out, 'utf8') !== content) writeFileSync(out, content);
