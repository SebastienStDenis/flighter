// Drives the service worker's page handling with a fake network and a fake cache:
// first the server answers, then it does not, then the copy it left is a day old.
const fs = require("fs");

const stores = new Map();
const caches = {
  open: async (name) => {
    if (!stores.has(name)) stores.set(name, new Map());
    const store = stores.get(name);
    return {
      match: async (request) => store.get(request.url),
      put: async (request, response) => void store.set(request.url, response),
      delete: async (request) => store.delete(request.url),
    };
  },
};
const self = { addEventListener() {}, location: { origin: "http://flighter.test" } };

let reachable = true;
global.fetch = async () => {
  if (!reachable) throw new TypeError("Failed to fetch");
  return new Response("<html>fresh</html>", {
    status: 200,
    headers: { "Content-Type": "text/html; charset=utf-8" },
  });
};

const source = fs.readFileSync(process.argv[2], "utf8");
const worker = new Function("self", "caches", source + "\nreturn { pageOrLastCopy };");
const { pageOrLastCopy } = worker(self, caches);

(async () => {
  const request = new Request("http://flighter.test/");
  const served = [];
  served.push(await (await pageOrLastCopy(request)).text());
  reachable = false;
  served.push(await (await pageOrLastCopy(request)).text());
  const store = stores.get("pages-v1");
  store.get(request.url).headers.set("x-flighter-saved-at", String(Date.now() - 25 * 3600 * 1000));
  served.push(await (await pageOrLastCopy(request)).text());
  console.log(JSON.stringify({ served, kept: store.has(request.url) }));
})();
