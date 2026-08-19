// Caches the shell so the app opens fullscreen from the home screen without waiting on
// the network, and caches no flight data at all. A gate number from an hour ago is
// worse than a page that says it could not reach the server, so every navigation and
// every API call goes to the network and is never stored.

const SHELL = "shell-v1";
const ASSETS = [
  "/static/app.css",
  "/static/htmx.min.js",
  "/static/icon.svg",
  "/static/manifest.json",
];

const OFFLINE_PAGE = `<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>Offline</title><link rel="stylesheet" href="/static/app.css"></head><body>
<main><div class="empty"><strong>No connection</strong>
Flight times are only shown live, never from a cache. Reconnect and reload.
<div class="actions" style="justify-content: center">
<a class="btn btn--primary" href="/">Try again</a></div></div></main></body></html>`;

self.addEventListener("install", (event) => {
  event.waitUntil(caches.open(SHELL).then((cache) => cache.addAll(ASSETS)));
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches
      .keys()
      .then((keys) => Promise.all(keys.filter((key) => key !== SHELL).map((key) => caches.delete(key))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (event) => {
  const request = event.request;
  if (request.method !== "GET") return;

  const url = new URL(request.url);
  if (url.origin !== self.location.origin) return;

  if (url.pathname.startsWith("/static/")) {
    event.respondWith(
      caches.match(request).then((hit) => hit || fetch(request))
    );
    return;
  }

  // Everything else is flight data: the list, a flight, the widget feed.
  event.respondWith(
    fetch(request, { cache: "no-store" }).catch(() =>
      request.mode === "navigate"
        ? new Response(OFFLINE_PAGE, { headers: { "Content-Type": "text/html; charset=utf-8" } })
        : Response.error()
    )
  );
});
