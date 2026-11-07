// Local frontend server on its own port: serves public/ and passes /api/* to the backend, so the
// browser only talks to one site and the session cookie stays first-party (no CORS).
// Usage: node dev-server.mjs   (PORT defaults to 5173, API_ORIGIN to http://127.0.0.1:8787)
// 127.0.0.1, not localhost: on Windows, localhost tries IPv6 first and adds about two seconds per call.
import { createReadStream, existsSync, readFileSync, statSync } from 'node:fs';
import { request as forward, createServer } from 'node:http';
import { extname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const PORT = Number(process.env.PORT ?? 5173);
const API = new URL(process.env.API_ORIGIN ?? 'http://127.0.0.1:8787');
const ROOT = fileURLToPath(new URL('./public/', import.meta.url));
const TYPES = { '.html': 'text/html; charset=utf-8', '.js': 'text/javascript; charset=utf-8', '.css': 'text/css; charset=utf-8', '.svg': 'image/svg+xml', '.json': 'application/json', '.png': 'image/png' };

// Same security headers production sends, from public/_headers.
const HEADERS = Object.fromEntries(readFileSync(join(ROOT, '_headers'), 'utf8').split('\n')
  .filter((line) => /^\s+\S+:/.test(line))
  .map((line) => line.trim().split(/:\s*(.*)/s).slice(0, 2))
  .filter(([name]) => name !== 'Strict-Transport-Security'));

createServer((req, res) => {
  if (req.url.startsWith('/api/')) {
    const upstream = forward({ host: API.hostname, port: API.port, method: req.method, path: req.url, headers: { ...req.headers, host: API.host } }, (answer) => {
      res.writeHead(answer.statusCode, answer.headers); // Redirects reach the browser as they are.
      answer.pipe(res);
    });
    upstream.on('error', () => { res.writeHead(502, { 'Content-Type': 'application/json' }).end(`{"error":"Backend is not running on ${API.origin}"}`); });
    req.pipe(upstream);
    return;
  }
  let file = resolve(ROOT, '.' + decodeURIComponent(new URL(req.url, 'http://x').pathname));
  // Only files inside public/; anything else gets the app page.
  if (!file.startsWith(ROOT) || !existsSync(file) || statSync(file).isDirectory()) file = join(ROOT, 'index.html');
  res.writeHead(200, { ...HEADERS, 'Content-Type': TYPES[extname(file)] ?? 'application/octet-stream', 'Cache-Control': 'no-cache' });
  createReadStream(file).pipe(res);
}).listen(PORT, '127.0.0.1', () => console.log(`Frontend on http://localhost:${PORT}, API from ${API.origin}`));
