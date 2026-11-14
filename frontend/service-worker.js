/* Build substitutes an exact public-asset allowlist and content-derived version.
 * Never cache API/SSE, credentials, mutations, query strings or arbitrary URLs. */
const CACHE = "openbot-shell-__BUILD_ID__";
const ASSETS = new Set(__PUBLIC_ASSETS__);
const PRECACHE = __PRECACHE__;
const ownCaches = async () => (await caches.keys()).filter((name) => name.startsWith("openbot-shell-"));

self.addEventListener("install", (event) => {
  event.waitUntil((async () => {
    try {
      const cache = await caches.open(CACHE);
      // Omit credentials even for public assets. Fail installation on a partial release.
      await Promise.all(PRECACHE.map(async (url) => {
        const request = new Request(url, { credentials: "omit", cache: "reload" });
        const response = await fetch(request);
        if (!response.ok || response.redirected || response.type !== "basic") throw new Error("Public shell unavailable");
        await cache.put(request, response);
      }));
    } catch (error) { await caches.delete(CACHE); throw error; }
  })());
});
self.addEventListener("activate", (event) => {
  event.waitUntil((async () => {
    // Keep one prior build for already-open tabs; never delete another app's caches.
    const previous = (await ownCaches()).filter((name) => name !== CACHE).slice(-1);
    for (const name of await ownCaches()) if (name !== CACHE && !previous.includes(name)) await caches.delete(name);
    await self.clients.claim();
  })());
});
self.addEventListener("message", (event) => {
  if (event.data?.type === "ACTIVATE_UPDATE") event.waitUntil(self.skipWaiting());
});

self.addEventListener("fetch", (event) => {
  const request = event.request;
  const url = new URL(request.url);
  if (request.method !== "GET" || url.origin !== self.location.origin || url.search ||
      request.headers.has("authorization") || request.headers.has("x-api-key") ||
      url.pathname === "/api" || url.pathname.startsWith("/api/")) return;
  if (request.mode === "navigate" && !url.pathname.split("/").pop().includes(".")) {
    event.respondWith((async () => {
      try {
        const response = await fetch(request);
        if (response.status < 500) return response;
      } catch { /* Offline launch: public shell only; API reads still go to the network. */ }
      const cache = await caches.open(CACHE);
      return await cache.match("/index.html") || await cache.match("/offline.html") || Response.error();
    })());
    return;
  }
  // Permit old fingerprinted assets only if already cached by one of our builds.
  // Everything else outside the generated allowlist goes straight to the network.
  const fingerprinted = /^\/assets\/.+-[\w-]{8,}\.(js|css|woff2?)$/.test(url.pathname);
  if (!ASSETS.has(url.pathname) && !fingerprinted) return;
  event.respondWith((async () => {
    let cache;
    try {
      cache = await caches.open(CACHE);
      const cached = await cache.match(request);
      if (cached) return cached;
      if (fingerprinted) {
        for (const name of await ownCaches()) {
          const old = await (await caches.open(name)).match(request);
          if (old) return old;
        }
      }
    } catch { /* Storage unavailable: fall through to the network. */ }
    const response = await fetch(request);
    if (cache && ASSETS.has(url.pathname) && response.ok && response.type === "basic" && !response.redirected) {
      // Failure/full storage must never prevent a successful network response.
      event.waitUntil(cache.put(request, response.clone()).catch(() => {}));
    }
    return response;
  })());
});
