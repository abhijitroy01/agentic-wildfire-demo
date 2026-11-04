/* Browser globals for the node tests in ui/scripts/test: tab storage, address bar, history, window events and a
 * scripted fetch, plus the broker's workspace-config answers. Each browser() call replaces the globals. */
const origin = "https://lab.example.test";
const local = { access_mode: "local", agent_configured: true, geocoder_configured: true };
const publicDemo = { access_mode: "public_demo", agent_configured: true, geocoder_configured: true };
const json = (body, status = 200) => new Response(JSON.stringify(body),
  { status, headers: { "Content-Type": "application/json" } });
/** Answer GET /workspace-config with `workspace` (local development by default) and route the rest to `reply`. */
const brokerOr = (reply, workspace = local) => (url, init) =>
  (url.endsWith("/workspace-config") ? json(workspace) : reply(url, init));

/** Fresh browser globals for one test: tab storage, address bar, history, window events and fetch. */
function browser(href = `${origin}/`, route = () => json(local)) {
  const values = new Map();
  const storage = {
    getItem: key => (values.has(key) ? values.get(key) : null),
    setItem: (key, value) => { values.set(key, String(value)); },
    removeItem: key => { values.delete(key); },
  };
  let url = new URL(href);
  const env = { storage, route, reloads: 0, requests: [], events: new EventTarget() };
  const define = (name, value) => Object.defineProperty(globalThis, name, { value, configurable: true, writable: true });
  define("sessionStorage", storage);
  define("location", { get href() { return url.href; }, get origin() { return url.origin; },
    get search() { return url.search; }, get pathname() { return url.pathname; }, reload: () => { env.reloads += 1; } });
  define("history", { replaceState: (_state, _title, next) => { url = new URL(String(next), url); } });
  define("window", env.events);
  define("fetch", async (input, init = {}) => {
    env.requests.push({ url: String(input), init });
    return env.route(String(input), init);
  });
  return env;
}

/** A provider that never answers: like the browser's fetch, it rejects only when its signal aborts. Its own timer keeps
 * Node running (AbortSignal.timeout's does not) and gives up after 10 s, so an unbounded request fails its test. */
function hang(_url, init) {
  if (init.signal.aborted) return Promise.reject(init.signal.reason);
  return new Promise((_resolve, reject) => {
    const alive = setTimeout(() => reject(new Error("The request was never bounded")), 10_000);
    init.signal.addEventListener("abort", () => { clearTimeout(alive); reject(init.signal.reason); }, { once: true });
  });
}

module.exports = { browser, brokerOr, hang, json, local, origin, publicDemo };
