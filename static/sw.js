/* Shelfie service worker: lets the app open and books be read with no connection.
   Book text never changes, so it is cache-first. Everything else is network-first with a
   short timeout, so a phone that can't reach the server (away from home) falls back to
   the last copy quickly. Writes are never cached; the page queues them while offline. */
const V = "shelfie-v5"; // v5: find any book
const SHELL = ["/app", "/static/manifest.webmanifest", "/static/icon.svg"];

self.addEventListener("install", e => {
  e.waitUntil(caches.open(V + "-shell").then(c => c.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", e => {
  e.waitUntil((async () => {
    for (const k of await caches.keys()) if (!k.startsWith(V)) await caches.delete(k);
    await self.clients.claim();
  })());
});

self.addEventListener("fetch", e => {
  const req = e.request;
  if (req.method !== "GET") return;
  const url = new URL(req.url);
  if (url.origin === location.origin) {
    const p = url.pathname;
    if (p.startsWith("/api/text/")) return e.respondWith(cacheFirst(req, V + "-books"));
    if (p.startsWith("/api/cover")) return e.respondWith(cacheFirst(req, V + "-covers", 400));
    // annotations: the page keeps its own offline copy; search/export are only useful live
    if (p.startsWith("/api/books/") || p.startsWith("/api/search") || p.startsWith("/api/find") || p.startsWith("/api/listen") || p.startsWith("/api/export")) return;
    if (p.startsWith("/api/")) return e.respondWith(networkFirst(req, V + "-api"));
    return e.respondWith(networkFirst(req, V + "-shell"));
  }
  if (url.hostname === "fonts.googleapis.com" || url.hostname === "fonts.gstatic.com")
    return e.respondWith(cacheFirst(req, V + "-fonts"));
  if (req.destination === "image") return e.respondWith(cacheFirst(req, V + "-covers", 150));
});

async function cacheFirst(req, name, max) {
  const c = await caches.open(name);
  const hit = await c.match(req);
  if (hit) return hit;
  const res = await fetch(req);
  if (res.ok || res.type === "opaque") {
    await c.put(req, res.clone());
    if (max) trim(c, max);
  }
  return res;
}

async function networkFirst(req, name, ms = 3000) {
  const c = await caches.open(name);
  const net = fetch(req).then(res => {
    if (res.ok) c.put(req, res.clone());
    return res;
  });
  net.catch(() => {});
  try {
    const res = await Promise.race([net, new Promise(r => setTimeout(r, ms))]);
    if (res) return res;
  } catch {}
  // offline, any app page falls back to the app shell (never the website at /)
  const hit = (await c.match(req)) || (req.mode === "navigate" && (await c.match("/app")));
  if (hit) return fromCache(hit);
  return net;
}

// marks a response as stale so the page can show that it's offline
function fromCache(res) {
  const h = new Headers(res.headers);
  h.set("X-Shelfie-Offline", "1");
  return new Response(res.body, { status: res.status, statusText: res.statusText, headers: h });
}

async function trim(c, max) {
  const keys = await c.keys();
  for (const k of keys.slice(0, Math.max(0, keys.length - max))) await c.delete(k);
}
