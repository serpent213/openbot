import { pluginEvents } from "./events";
import installed from "virtual:openbot-plugins";
import type { FrontendPlugin } from "./types";

export function validatePlugins(plugins: FrontendPlugin[]) {
  const ids = new Set<string>();
  const paths = new Set(["/", "/inbox", "/threads", "/bots", "/settings", "/scheduled", "/lab"]);
  for (const plugin of plugins) {
    if (plugin.apiVersion !== 1 || !/^[a-z][a-z0-9_]{0,31}$/.test(plugin.id) || ids.has(plugin.id)) throw new Error("Invalid or duplicate UI plugin: " + plugin.id);
    ids.add(plugin.id);
    for (const page of plugin.pages ?? []) {
      if (!/^\/[a-z][a-z0-9_-]*$/.test(page.path) || paths.has(page.path)) throw new Error("Invalid or duplicate plugin page: " + page.path);
      paths.add(page.path);
    }
  }
  return plugins;
}
export const plugins = validatePlugins(installed);
export const pluginPages = plugins.flatMap(p => p.pages ?? []);
for (const plugin of plugins) for (const event of plugin.events ?? []) pluginEvents.add(event);
