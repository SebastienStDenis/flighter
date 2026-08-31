// Caches the shell so the app opens fullscreen from the home screen without waiting on
// the network, and keeps the last copy of every page so the board still opens when
// there is no network at all. A copy is only ever the fallback: every page request goes
// to the server first, and the copy is served when the server cannot be reached. The
// page itself says how old it is, so a gate number from an hour ago reads as one.

// The release is in the worker's own address, put there as it was registered, so a new
// build is a new worker and its caches start over: activation below drops the old
// release's shell and pages wholesale rather than trusting anything to refresh them.
const RELEASE = new URL(self.location.href || self.location.origin).searchParams.get("v") || "v1";
const SHELL = `shell-${RELEASE}`;
const PAGES = `pages-${RELEASE}`;
const AIRLINE_LOGOS = "airline-logos-v1";
const AIRLINE_LOGO_ORIGIN = "https://www.gstatic.com";
const AIRLINE_LOGO_PATH = "/flights/airline_logos/70px/";
const ASSETS = [
  "/static/flighter.css",
  "/static/basecoat.min.js",
  "/static/basecoat-tabs.min.js",
  "/static/fonts/satoshi-var.woff2",
  "/static/fonts/b612-mono-latin-400.woff2",
  "/static/fonts/b612-mono-latin-700.woff2",
  "/static/icon.svg",
  "/static/manifest.json",
];

// How long a page request waits on the network before the last copy is shown instead. A
// phone on dead wifi does not error, it hangs, and this is the difference between the
// board and a white screen.
const PATIENCE_MS = 4000;

// A copy older than this is not shown even offline: a flight stopped a day ago should
// not come back, and nothing about a flight from yesterday is worth knowing now.
const KEEP_MS = 24 * 60 * 60 * 1000;
const SAVED_AT = "x-flighter-saved-at";

const OFFLINE_PAGE = `<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>Offline</title><link rel="stylesheet" href="/static/flighter.css"></head>
<body><main class="mx-auto w-full max-w-lg px-4 pt-16"><section class="empty">
<header><h2>No connection</h2><p>Check your connection and try again.</p></header>
<footer><a class="btn" href="/">Try again</a></footer>
</section></main></body></html>`;

self.addEventListener("install", (event) => {
  // Fetched past the browser's own cache: this install exists because the app changed,
  // and Safari's copy of an asset can outlive the release it came from, so a shell
  // built through it would be the new worker guarding the old files.
  event.waitUntil(
    caches
      .open(SHELL)
      .then((cache) => cache.addAll(ASSETS.map((asset) => new Request(asset, { cache: "reload" }))))
  );
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches
      .keys()
      .then((keys) =>
        Promise.all(
          keys
            .filter((key) => key !== SHELL && key !== PAGES && key !== AIRLINE_LOGOS)
            .map((key) => caches.delete(key))
        )
      )
      .then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (event) => {
  const request = event.request;
  if (request.method !== "GET") return;

  const url = new URL(request.url);
  if (url.origin === AIRLINE_LOGO_ORIGIN && url.pathname.startsWith(AIRLINE_LOGO_PATH)) {
    event.respondWith(airlineLogoOrNetwork(request));
    return;
  }

  if (url.origin !== self.location.origin) return;

  if (url.pathname.startsWith("/static/")) {
    // Served from the cache so the shell paints offline and instantly, then refreshed
    // in the background - asking the server rather than the browser's cache, whose
    // copy can be exactly as old as the one being refreshed.
    event.respondWith(
      caches.open(SHELL).then((cache) =>
        cache.match(request).then((hit) => {
          const fresh = fetch(request, { cache: "no-cache" })
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

  if (request.mode === "navigate") {
    event.respondWith(pageOrLastCopy(request));
    return;
  }

  // Anything else is a feed, and a feed is never served stale.
  event.respondWith(fetch(request, { cache: "no-store" }).catch(() => Response.error()));
});

async function airlineLogoOrNetwork(request) {
  let cache;
  try {
    cache = await caches.open(AIRLINE_LOGOS);
    const hit = await cache.match(request);
    if (hit) return hit;
  } catch {}

  const response = await fetch(request);
  if (cache && (response.type === "opaque" || response.ok)) {
    try {
      await cache.put(request, response.clone());
    } catch {}
  }
  return response;
}

async function pageOrLastCopy(request) {
  const cache = await caches.open(PAGES);
  try {
    const response = await withPatience(fetch(request, { cache: "no-store" }));
    // A redirect is the page after a form post, and the browser refuses a redirected
    // response served back to a navigation, so only a page that came straight is kept.
    if (response.ok && !response.redirected) await cache.put(request, stamped(response));
    return response;
  } catch {
    // Which tab a page was left on is in its address, and that is not a different page:
    // the last copy of the board answers for the board whichever tab the address names.
    const copy = await cache.match(request, { ignoreSearch: true });
    if (copy && Date.now() - Number(copy.headers.get(SAVED_AT)) < KEEP_MS) return copy;
    if (copy) await cache.delete(request, { ignoreSearch: true });
    return new Response(OFFLINE_PAGE, { headers: { "Content-Type": "text/html; charset=utf-8" } });
  }
}

function withPatience(pending) {
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error("no answer")), PATIENCE_MS);
    pending.then(resolve, reject).finally(() => clearTimeout(timer));
  });
}

function stamped(response) {
  const copy = new Response(response.clone().body, response);
  copy.headers.set(SAVED_AT, String(Date.now()));
  return copy;
}
