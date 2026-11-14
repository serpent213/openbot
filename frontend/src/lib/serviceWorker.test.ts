import { readFileSync } from "node:fs";
import { runInNewContext } from "node:vm";
import { describe, expect, it, vi } from "vitest";

function worker() {
  const handlers: Record<string, (event: any) => void> = {};
  const stores = new Map<string, Map<string, Response>>();
  const key = (input: any) => new URL(typeof input === "string" ? input : input.url, "https://openbot.test").pathname;
  const caches = {
    keys: async () => [...stores.keys()],
    delete: vi.fn(async (name: string) => stores.delete(name)),
    open: async (name: string) => {
      if (!stores.has(name)) stores.set(name, new Map());
      const store = stores.get(name)!;
      return { match: async (request: any) => store.get(key(request)), put: async (request: any, response: Response) => { store.set(key(request), response); } };
    },
  };
  const response = (text: string) => {
    const result = new Response(text);
    Object.defineProperty(result, "type", { value: "basic" });
    return result;
  };
  const fetch = vi.fn(async () => response("network"));
  const skipWaiting = vi.fn(async () => {});
  const claim = vi.fn(async () => {});
  class RelativeRequest extends Request { constructor(url: string, init?: RequestInit) { super(new URL(url, "https://openbot.test"), init); } }
  const source = readFileSync("service-worker.js", "utf8").replace("__BUILD_ID__", "test")
    .replace("__PUBLIC_ASSETS__", JSON.stringify(["/assets/app-abcdefgh.js", "/theme-init.js"]))
    .replace("__PRECACHE__", JSON.stringify(["/index.html", "/offline.html", "/assets/app-abcdefgh.js"]));
  runInNewContext(source, { self: { location: { origin: "https://openbot.test" }, addEventListener: (name: string, fn: any) => { handlers[name] = fn; }, clients: { claim }, skipWaiting }, caches, fetch, URL, Request: RelativeRequest, Response, Set });
  const lifecycle = async (name: string, extra = {}) => {
    let work: Promise<any> | undefined;
    handlers[name]({ waitUntil: (promise: Promise<any>) => { work = promise; }, ...extra });
    await work;
  };
  const request = (path: string, extra = {}) => {
    const respondWith = vi.fn();
    const waitUntil = vi.fn();
    handlers.fetch({ request: { url: new URL(path, "https://openbot.test").href, method: "GET", headers: new Headers(), mode: "cors", ...extra }, respondWith, waitUntil });
    return { response: respondWith.mock.calls[0]?.[0] as Promise<Response> | undefined, respondWith, waitUntil };
  };
  return { stores, caches, fetch, skipWaiting, claim, lifecycle, request, response };
}

describe("service worker security and lifecycle", () => {
  it("never intercepts APIs/SSE, authenticated requests, mutations, queries or foreign origins", () => {
    const sw = worker();
    for (const [url, options] of [
      ["/api/v1/events", {}], ["/api/v1/sandbox-credentials", {}], ["/api", { mode: "navigate" }],
      ["/assets/app-abcdefgh.js?api_key=not-a-real-key", {}], ["/threads/t?x=1", { mode: "navigate" }],
      ["https://other.test/assets/app-abcdefgh.js", {}], ["/private.txt", {}],
      ["/theme-init.js", { method: "POST" }], ["/theme-init.js", { headers: new Headers({ "X-API-Key": "fake" }) }],
      ["/theme-init.js", { headers: new Headers({ Authorization: "fake" }) }],
    ] as const) expect(sw.request(url, options).respondWith).not.toHaveBeenCalled();
    expect(sw.fetch).not.toHaveBeenCalled();
  });
  it("installs anonymously, never activates an update without consent, and preserves other caches", async () => {
    const sw = worker();
    await sw.caches.open("unrelated"); await sw.caches.open("openbot-shell-oldest"); await sw.caches.open("openbot-shell-previous");
    await sw.lifecycle("install");
    for (const [request] of sw.fetch.mock.calls as unknown as [Request][]) expect(request.credentials).toBe("omit");
    expect(sw.skipWaiting).not.toHaveBeenCalled();
    await sw.lifecycle("activate");
    expect(sw.stores.has("unrelated")).toBe(true);
    expect(sw.stores.has("openbot-shell-previous")).toBe(true);
    expect(sw.stores.has("openbot-shell-oldest")).toBe(false);
    await sw.lifecycle("message", { data: { type: "ACTIVATE_UPDATE" } });
    expect(sw.skipWaiting).toHaveBeenCalledOnce();
  });
  it("falls back to public shell for offline navigation, but does not cache navigation responses", async () => {
    const sw = worker();
    await sw.lifecycle("install");
    sw.fetch.mockRejectedValue(new TypeError("offline"));
    const response = await sw.request("/threads/t", { mode: "navigate" }).response;
    expect(await response?.text()).toBe("network");
    expect(sw.stores.get("openbot-shell-test")?.has("/threads/t")).toBe(false);
  });
  it("retains the prior build on failed installation and rejects redirects", async () => {
    const sw = worker();
    await sw.caches.open("openbot-shell-previous");
    const redirected = sw.response("login");
    Object.defineProperty(redirected, "redirected", { value: true });
    sw.fetch.mockResolvedValue(redirected);
    await expect(sw.lifecycle("install")).rejects.toThrow("Public shell unavailable");
    expect(sw.stores.has("openbot-shell-test")).toBe(false);
    expect(sw.stores.has("openbot-shell-previous")).toBe(true);
  });
  it("serves cached old fingerprinted assets but never caches arbitrary assets", async () => {
    const sw = worker();
    const old = await sw.caches.open("openbot-shell-previous");
    await old.put("/assets/old-abcdefgh.js", sw.response("old"));
    expect(await (await sw.request("/assets/old-abcdefgh.js").response)?.text()).toBe("old");
    await sw.request("/assets/unknown-abcdefgh.js").response;
    expect(sw.stores.get("openbot-shell-test")?.has("/assets/unknown-abcdefgh.js")).toBe(false);
  });
});
