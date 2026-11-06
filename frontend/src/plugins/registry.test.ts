import { describe, expect, it, vi } from "vitest";
import { validatePlugins } from "./registry";
import type { FrontendPlugin } from "./types";
vi.mock("virtual:openbot-plugins", () => ({ default: [] }));
const page = { path: "/example", title: "Example", component: () => null };
const plugin: FrontendPlugin = { id: "example", apiVersion: [1, 1], pages: [page] };
describe("UI plugin registry", () => {
  it("accepts explicitly compiled pages", () => {
    expect(validatePlugins([plugin])).toEqual([plugin]);
  });
  it("rejects incompatible versions, duplicate IDs and routes", () => {
    expect(() => validatePlugins([{ ...plugin, apiVersion: 2 } as unknown as FrontendPlugin])).toThrow();
    expect(() => validatePlugins([plugin, plugin])).toThrow();
    expect(() => validatePlugins([plugin, { ...plugin, id: "second" }])).toThrow();
    expect(() => validatePlugins([{ ...plugin, pages: [{ ...page, path: "/bots" }] }])).toThrow();
    expect(() => validatePlugins([{ ...plugin, pages: [{ ...page, path: "/:anything" }] }])).toThrow();
  });
});
