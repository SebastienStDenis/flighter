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
  "/static/fonts/manrope-latin-var.woff2",
  "/static/fonts/jetbrains-mono-latin-var.woff2",
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
    return new Response(offlinePage(request.url), {
      headers: { "Content-Type": "text/html; charset=utf-8" },
    });
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

// With no copy to fall back to, the app draws the page itself rather than a notice on a
// blank screen: the bar at the top is still there, so the board, the email and the
// settings are all one tap away and any of them the cache can answer for opens. The
// page is served at the address that was asked for, so Try again is a link to that same
// address - it asks for the page a person is standing on rather than sending them home.
//
// It is drawn out of the app's own classes, which are in the stylesheet because the
// templates use them; nothing here may reach for a utility no template does, or the
// build will not have generated it.
function offlinePage(href) {
  const url = new URL(href);
  const here = url.pathname;
  // The same reading of the address the header does: a flight is one level down from
  // the board, and /f/new is the add box rather than a flight.
  const flight = here.startsWith("/f/") && here !== "/f/new";
  const board = here === "/" || flight;
  const mail = here === "/mail" || here.startsWith("/mail/");
  const settings = here.startsWith("/settings");
  // Which tab the board was left on is not kept: the copy of the board that answers
  // offline is the one that was saved, whatever tab its address named.
  const back = flight
    ? `<a class="btn -ml-2.5 mb-2 self-start" data-size="sm" data-variant="ghost" href="/">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"
           stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
        <path d="m15 18-6-6 6-6"/>
      </svg>
      Flights
    </a>
`
    : "";
  return `<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>No connection</title>
<meta name="theme-color" content="#f5f7f9" media="(prefers-color-scheme: light)">
<meta name="theme-color" content="#0a0c11" media="(prefers-color-scheme: dark)">
<link rel="icon" href="/static/icon.svg" type="image/svg+xml">
<link rel="stylesheet" href="/static/flighter.css">
</head>
<body class="flex min-h-dvh flex-col">
<header class="sticky top-0 z-20 border-b bg-background/85 backdrop-blur"
        style="padding-top: env(safe-area-inset-top)">
  <nav class="topbar mx-auto flex h-14 max-w-lg items-center gap-1 px-3" aria-label="Sections">
    <a class="btn text-lg" data-size="lg" href="/" data-variant="ghost"${current(board)}>
      <span class="brand">
        <span class="brand-name">Flighter</span>
        <svg class="brand-plane size-[1.1em]" viewBox="0 0 24 24" fill="currentColor"
             aria-hidden="true">
          <path transform="rotate(90 12 12)"
                d="M21 15.5 13.5 11V4.2a1.5 1.5 0 0 0-3 0V11L3 15.5v2l7.5-2.2v4.4L8 21.3V23l4-1.2 4 1.2v-1.7l-2.5-1.6v-4.4l7.5 2.2z"/>
        </svg>
        <span class="brand-ahead" aria-hidden="true"></span>
      </span>
    </a>
    <!-- No + here. The box it opens posts a flight to the server, and there is no
         server, so the one control on the bar that cannot do its job is left off. -->
    <a class="btn ml-auto" data-size="icon-lg" href="/mail" aria-label="Email"
       data-variant="${mail ? "secondary" : "ghost"}"${current(mail)}>
      <!-- Plain, never marked: what is waiting on a person is a count from the server. -->
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"
           stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
        <rect width="20" height="16" x="2" y="4" rx="2"/>
        <path d="m22 7-8.97 5.7a1.94 1.94 0 0 1-2.06 0L2 7"/>
      </svg>
    </a>
    <a class="btn" data-size="icon-lg" href="/settings" aria-label="Settings"
       data-variant="${settings ? "secondary" : "ghost"}"${current(settings)}>
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"
           stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
        <path d="M9.671 4.136a2.34 2.34 0 0 1 4.659 0 2.34 2.34 0 0 0 3.319 1.915 2.34 2.34 0 0 1 2.33 4.033 2.34 2.34 0 0 0 0 3.831 2.34 2.34 0 0 1-2.33 4.033 2.34 2.34 0 0 0-3.319 1.915 2.34 2.34 0 0 1-4.659 0 2.34 2.34 0 0 0-3.32-1.915 2.34 2.34 0 0 1-2.33-4.033 2.34 2.34 0 0 0 0-3.831A2.34 2.34 0 0 1 6.35 6.051a2.34 2.34 0 0 0 3.319-1.915"/>
        <circle cx="12" cy="12" r="3"/>
      </svg>
    </a>
  </nav>
</header>
<main class="mx-auto flex w-full max-w-lg flex-1 flex-col px-4 pt-4">
${back}<section class="empty mt-8">
  <header>
    <figure>
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"
           stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
        <path d="M12 20h.01"/>
        <path d="M8.5 16.429a5 5 0 0 1 7 0"/>
        <path d="M5 12.859a10 10 0 0 1 5.17-2.69"/>
        <path d="M19 12.859a10 10 0 0 0-2.007-1.523"/>
        <path d="M2 8.82a15 15 0 0 1 4.177-2.643"/>
        <path d="M22 8.82a15 15 0 0 0-11.288-3.764"/>
        <path d="m2 2 20 20"/>
      </svg>
    </figure>
    <h2>No connection</h2>
    <p>Check your connection and try again.</p>
  </header>
  <footer><a class="btn" href="${attribute(here + url.search)}">Try again</a></footer>
</section>
</main>
</body>
</html>`;
}

function current(here) {
  return here ? ' aria-current="page"' : "";
}

function attribute(value) {
  return value
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}
