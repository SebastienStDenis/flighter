// Drives airline logo requests through the worker with an opaque network response,
// then takes the network offline and checks the cached response is reused.
const fs = require("fs");

const stores = new Map();
let storageFailure;
const caches = {
  open: async (name) => {
    if (storageFailure === "open") throw new Error("cache unavailable");
    if (!stores.has(name)) stores.set(name, new Map());
    const store = stores.get(name);
    return {
      match: async (request) => {
        if (storageFailure === "match") throw new Error("cache unavailable");
        return store.get(request.url);
      },
      put: async (request, response) => {
        if (storageFailure === "put") throw new Error("cache unavailable");
        store.set(request.url, response);
      },
    };
  },
  keys: async () => [...stores.keys()],
  delete: async (name) => stores.delete(name),
};

const listeners = {};
const self = {
  addEventListener: (name, listener) => void (listeners[name] = listener),
  clients: { claim: async () => {} },
  location: { origin: "https://flighter.test" },
  skipWaiting() {},
};

class FakeResponse {
  constructor(body, type, ok) {
    this.body = body;
    this.type = type;
    this.ok = ok;
  }

  clone() {
    return new FakeResponse(this.body, this.type, this.ok);
  }
}

let reachable = true;
let fetches = 0;
global.fetch = async () => {
  fetches += 1;
  if (!reachable) throw new TypeError("Failed to fetch");
  return networkResponse;
};
let networkResponse = new FakeResponse("logo", "opaque", false);

const source = fs.readFileSync(process.argv[2], "utf8");
new Function("self", "caches", source)(self, caches);

async function dispatch(url) {
  let response;
  listeners.fetch({
    request: { method: "GET", url },
    respondWith: (pending) => void (response = Promise.resolve(pending)),
  });
  return response ? { handled: true, response: await response } : { handled: false };
}

(async () => {
  const logo = "https://www.gstatic.com/flights/airline_logos/70px/AC.png";
  const online = await dispatch(logo);
  reachable = false;
  const offline = await dispatch(logo);
  let missingFailed = false;
  try {
    await dispatch("https://www.gstatic.com/flights/airline_logos/70px/UA.png");
  } catch {
    missingFailed = true;
  }

  reachable = true;
  networkResponse = new FakeResponse("not found", "basic", false);
  const failed = "https://www.gstatic.com/flights/airline_logos/70px/ZZ.png";
  await dispatch(failed);

  networkResponse = new FakeResponse("storage fallback", "opaque", false);
  const storageFailures = [];
  for (const operation of ["open", "match", "put"]) {
    storageFailure = operation;
    const result = await dispatch(
      `https://www.gstatic.com/flights/airline_logos/70px/${operation}.png`
    );
    storageFailures.push(result.response.body);
  }
  storageFailure = undefined;

  const ignored = await Promise.all([
    dispatch("https://example.com/flights/airline_logos/70px/AC.png"),
    dispatch("http://www.gstatic.com/flights/airline_logos/70px/AC.png"),
    dispatch("https://www.gstatic.com/flights/airline_logos/100px/AC.png"),
  ]);

  stores.set("old-cache", new Map());
  let activation;
  listeners.activate({ waitUntil: (pending) => void (activation = pending) });
  await activation;

  const logoStore = stores.get("airline-logos-v1");
  console.log(
    JSON.stringify({
      online: { handled: online.handled, body: online.response.body },
      offline: { handled: offline.handled, body: offline.response.body },
      fetches,
      cachedType: logoStore.get(logo).type,
      cachedFailure: logoStore.has(failed),
      missingFailed,
      storageFailures,
      ignored: ignored.map(({ handled }) => handled),
      caches: [...stores.keys()],
    })
  );
})();
