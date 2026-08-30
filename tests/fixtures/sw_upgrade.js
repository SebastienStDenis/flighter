// Drives an upgrade through the worker: a new build registers a worker whose address
// carries the new release, and by the time it is done the old release's caches are
// gone, the new shell was fetched past the browser's cache, and a served asset is
// refreshed by asking the server rather than that same cache.
const fs = require("fs");

const stores = new Map();
const caches = {
  open: async (name) => {
    if (!stores.has(name)) stores.set(name, new Map());
    const store = stores.get(name);
    return {
      addAll: async (requests) => requests.forEach((request) => store.set(request.url, request)),
      match: async (request) => store.get(request.url),
      put: async (request, response) => void store.set(request.url, response),
    };
  },
  keys: async () => [...stores.keys()],
  delete: async (name) => stores.delete(name),
};

// What the release being replaced left behind, as activation finds it.
stores.set("shell-oldbuild", new Map());
stores.set("pages-oldbuild", new Map());
stores.set("airline-logos-v1", new Map());

const listeners = {};
const self = {
  addEventListener: (name, listener) => void (listeners[name] = listener),
  clients: { claim: async () => {} },
  location: { href: "https://flighter.test/sw.js?v=newbuild", origin: "https://flighter.test" },
  skipWaiting() {},
};

// Requests only carry what the worker is being watched for: the address and how the
// browser's own cache is to be treated on the way to the server.
class FakeRequest {
  constructor(url, options) {
    this.url = new URL(url, self.location.origin).toString();
    this.method = "GET";
    this.cache = (options && options.cache) || "default";
  }
}
global.Request = FakeRequest;

const fetched = [];
global.fetch = async (request, options) => {
  fetched.push({ url: request.url, cache: (options && options.cache) || request.cache });
  return { ok: true, clone: () => "fresh copy" };
};

const source = fs.readFileSync(process.argv[2], "utf8");
new Function("self", "caches", source)(self, caches);

(async () => {
  let installing;
  listeners.install({ waitUntil: (pending) => void (installing = pending) });
  await installing;

  let activating;
  listeners.activate({ waitUntil: (pending) => void (activating = pending) });
  await activating;

  const shell = stores.get("shell-newbuild") || new Map();
  const precached = [...shell.values()].map((request) => request.cache);

  // An asset the install put in the shell: answered from it, refreshed behind it.
  let served;
  listeners.fetch({
    request: new FakeRequest("/static/flighter.css"),
    respondWith: (pending) => void (served = Promise.resolve(pending)),
  });
  await served;

  console.log(
    JSON.stringify({
      caches: [...stores.keys()].sort(),
      precached,
      refresh: fetched[fetched.length - 1],
    })
  );
})();
