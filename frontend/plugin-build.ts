import { resolve, dirname } from "node:path";
import type { Plugin } from "vite";

export function pluginBuild(): Plugin {
  const files = (process.env.OPENBOT_FRONTEND_PLUGINS ?? "").split(",").filter(Boolean).map(p => resolve(p));
  const peers = ["react", "react-dom", "react-router-dom", "@tanstack/react-query"];
  return {
    name: "openbot-plugins",
    enforce: "pre",
    transform(code, id) {
      if (id.endsWith(".css") && code.includes('@import "tailwindcss"')) {
        return code + "\n" + files.map(file => `@source ${JSON.stringify(dirname(file))};`).join("\n");
      }
    },
    config: () => ({
      resolve: { alias: { "@openbot/plugin-api": resolve("src/plugins/api.ts") }, dedupe: peers },
      server: { fs: { allow: [process.cwd(), ...files.map(dirname)] } },
    }),
    async resolveId(id, importer) {
      if (id === "virtual:openbot-plugins") return "\0" + id;
      if (importer && files.some(file => importer.startsWith(dirname(file))) && peers.some(p => id === p || id.startsWith(p + "/"))) {
        return this.resolve(id, resolve("src/plugins/api.ts"), { skipSelf: true });
      }
    },
    load(id) {
      if (id !== "\0virtual:openbot-plugins") return;
      return files.map((file, i) => `import p${i} from ${JSON.stringify(file)};`).join("\n")
        + `\nexport default [${files.map((_, i) => `p${i}`).join(",")}];`;
    },
  };
}
