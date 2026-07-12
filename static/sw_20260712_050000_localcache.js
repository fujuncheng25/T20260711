const CACHE_NAME = "tmall-local-static-v20260712-073000";
const RETENTION_MS = 90 * 24 * 60 * 60 * 1000;

function shouldCacheRequest(request) {
  if (request.method !== "GET") {
    return false;
  }

  const url = new URL(request.url);
  if (url.origin !== self.location.origin) {
    return false;
  }

  const path = url.pathname;
  if (path === "/sw.js") {
    return false;
  }
  if (path.startsWith("/api/")) {
    return false;
  }
  if (path.startsWith("/admin/database/download")) {
    return false;
  }

  if (request.mode === "navigate") {
    return true;
  }

  if (path.startsWith("/static/") || path.startsWith("/uploads/")) {
    return true;
  }

  return ["document", "script", "style", "image", "font"].includes(request.destination);
}

async function stampResponse(response) {
  const headers = new Headers(response.headers);
  headers.set("x-local-cached-at", String(Date.now()));

  return new Response(response.body, {
    status: response.status,
    statusText: response.statusText,
    headers,
  });
}

function isFresh(response) {
  const raw = response.headers.get("x-local-cached-at");
  const cachedAt = Number(raw || "0");

  if (!Number.isFinite(cachedAt) || cachedAt <= 0) {
    return true;
  }

  return (Date.now() - cachedAt) <= RETENTION_MS;
}

self.addEventListener("install", (event) => {
  event.waitUntil(self.skipWaiting());
});

self.addEventListener("activate", (event) => {
  event.waitUntil((async () => {
    const names = await caches.keys();
    await Promise.all(names.filter((name) => name !== CACHE_NAME).map((name) => caches.delete(name)));
    await self.clients.claim();
  })());
});

self.addEventListener("fetch", (event) => {
  const request = event.request;
  if (!shouldCacheRequest(request)) {
    return;
  }

  event.respondWith((async () => {
    const cache = await caches.open(CACHE_NAME);
    const cached = await cache.match(request);

    if (cached && isFresh(cached)) {
      return cached;
    }

    try {
      const network = await fetch(request);
      if (network && network.ok) {
        const stamped = await stampResponse(network.clone());
        await cache.put(request, stamped);
      }
      return network;
    } catch (error) {
      if (cached) {
        return cached;
      }
      throw error;
    }
  })());
});
