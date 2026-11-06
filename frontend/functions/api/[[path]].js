// Passes /api/* to the backend Worker. The browser only ever talks to this site,
// so the session cookie stays first-party and no CORS is needed.
export function onRequest({ request, env }) {
  const url = new URL(request.url);
  const headers = new Headers(request.headers);
  // Compression is negotiated between this site and the browser, not passed through from the backend.
  headers.delete('Accept-Encoding');
  return fetch(new URL(url.pathname + url.search, env.API_ORIGIN), {
    method: request.method,
    headers,
    body: request.body,
    redirect: 'manual', // Sign-in redirects must reach the browser, not be followed here.
  });
}
