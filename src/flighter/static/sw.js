// Caches the shell so the app opens fullscreen from the home screen without waiting on
// the network, and caches no flight data at all. A gate number from an hour ago is
// worse than a page that says it could not reach the server, so every navigation and
// every API call goes to the network and is never stored.

const SHELL = "shell-v4";
const ASSETS = [
  "/static/flighter.css",
  "/static/htmx.min.js",
  "/static/basecoat.min.js",
  "/static/basecoat-tabs.min.js",
  "/static/fonts/manrope-latin-var.woff2",
  "/static/fonts/jetbrains-mono-latin-var.woff2",
  "/static/icon.svg",
  "/static/manifest.json",
];

const OFFLINE_PAGE = `<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>Offline</title><link rel="stylesheet" href="/static/flighter.css"></head>
<body><main class="mx-auto w-full max-w-lg px-4 pt-16"><section class="empty">
<header><h2>No connection</h2><p>Check your connection and try again.</p></header>
<footer><a class="btn" href="/">Try again</a></footer>
</section></main></body></html>`;

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
    // Served from the cache so the shell paints offline and instantly, then refreshed in
    // the background: an upgrade that ships a new stylesheet must not be invisible until
    // somebody remembers to rename the cache.
    event.respondWith(
      caches.open(SHELL).then((cache) =>
        cache.match(request).then((hit) => {
          const fresh = fetch(request)
            .then((response) => {
              if (response.ok) cache.put(request, response.clone());
              return response;
            })
            .catch(() => hit);
          return hit || fresh;
        })
      )
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
