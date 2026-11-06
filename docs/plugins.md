# Plugin API v1

Plugins are trusted, operator-installed Python packages. Core has no dependency on
particular plugins. Install packages in the backend environment, then configure:

```sh
PLUGIN_MODULES='["example_plugin"]'
PLUGIN_STATE_DIR=/var/lib/openbot/plugins
PLUGIN_CONFIG='{"example":{"some_option":"value"}}'
```

Restart the backend after changing these settings. Configuration is server-side;
plugins can provide their own authenticated settings page. Assign plugin tools and
optional approval flags in the normal bot editor. Disabling a plugin removes its
routes/tools at the next restart, but keeps its data and migration history.

## Backend contract

The configured module exports `plugin = Plugin(...)` from `openbot.plugin_api`:

```python
from contextlib import asynccontextmanager
from fastapi import APIRouter
from openbot.plugin_api import Plugin

router = APIRouter(prefix="/example")

@router.get("/status")
async def status():
    return {"ready": True}

@asynccontextmanager
async def lifespan(host):
    # Open resources here; close them in finally after yield.
    yield

plugin = Plugin(id="example", router=router, lifespan=lifespan)
```

Optional fields are `tools` (tuple of LangChain BaseTool objects), `requires`
(tuple of other plugin IDs), `migrations` (Path to an Alembic directory), and
`lifespan` (async context manager). `api_version` defaults to 1. Invalid versions,
duplicate IDs, missing/cyclic dependencies, duplicate tools and exact API route
collisions fail startup. Use a distinct route prefix; API routers mount beneath
`/api/v1` with core's API-key authentication. Tools use normal injected
`ToolRuntime[RunContext]`, rather than accepting actor identity from the model.

`PluginHost` supplies the async SQLAlchemy session factory, database URL, private
state directory, plugin configuration, process-local `state` dictionary, and
`publish(event, thread_id, data)`. Use `host_from_request(request, id)` in routes
and `host_from_runtime(runtime, id)` in tools. These are server-side facilities,
not capabilities to expose as unrestricted model arguments.

Plugins start in dependency order before workers and stop in reverse order after
workers. Imports should declare functionality without starting tasks. A lifecycle
hook is trusted code: operators must review any automation it starts. Event
publication is a best-effort UI notification, **not durable workflow delivery**.
Store state transitions transactionally; do not use events to trigger irreversible
work that needs retries or exactly-once guarantees.

## Persistence

Use a plugin-specific table prefix. Plugin Alembic migrations have independent
`plugin_<id>_version` tables. Their `env.py` consists of:

```python
from openbot.plugin_api import migration_environment
migration_environment()
```

The host runs migrations on its existing connection, supporting SQLite and
PostgreSQL. Provide explicit migrations for both dialects; no automatic schema
creation or downgrade runs on removal. `ModelBase` has separate ORM metadata and
ID-only foreign-key targets for `actors` and `threads`. It also exports
`TimestampMixin`, `UTCDateTime`, `new_id`, `utcnow`, `find_actor(session, handle)`
and `thread_exists(session, id)`. Do not modify core tables or depend on private
core models. Do not call create_all on plugin metadata.

Back up both the application database and `PLUGIN_STATE_DIR`; plugins may keep
files there. PostgreSQL roles or external stores may need additional backups as
documented by their plugin. Removing a bot does not automatically erase its
plugin files; removal/retention policy belongs to the plugin.

## Frontend contract

Compile selected entry files with the host (paths relative to the frontend cwd):

```sh
OPENBOT_FRONTEND_PLUGINS=../../example-plugin/frontend/index.ts pnpm build
```

The comma-separated list is explicit; there is no runtime download. The entry
exports a default `FrontendPlugin` from `@openbot/plugin-api`:

```tsx
import type { FrontendPlugin } from "@openbot/plugin-api";
const Page = () => <p>Example</p>;
export default {
  id: "example", apiVersion: 1,
  pages: [{ path: "/example", title: "Example", component: Page }],
  events: ["example.updated"],
} satisfies FrontendPlugin;
```

Pages use unique, single-segment paths. Core adds navigation and document titles.
The SDK exports the authenticated `request` function, `Api`, identity access,
`useBusEvents`, common UI components and types. `request(path, { actor: true })`
adds the selected operator identity header when needed. Text from tools, pages,
or stored data must not be treated as trusted model instructions.

React, React DOM, React Router and React Query are shared with the host. Tailwind
scans selected plugin directories. Install any other frontend dependencies
explicitly; avoid bundling a second copy of React. Build backend and frontend with
matching plugin sets. Plugins without pages need no frontend entry.

The plugin API is a compatibility contract, not process isolation. Existing shell
access can bypass tool-level permissions. Keep installation/configuration in
operator hands; API v1 intentionally has no package installer, marketplace, agent
self-enablement or automatic dispatcher.
