# Plugin API 1.1

Plugins are trusted, operator-installed Python packages. Core has no dependency on
particular plugins. Install packages in the backend environment, then configure:

```sh
PLUGIN_MODULES='["example_plugin"]'
PLUGIN_STATE_DIR=/var/lib/openbot/plugins
PLUGIN_CONFIG='{"example":{"some_option":"value"}}'
```

Restart the backend after changing these settings. Configuration is server-side;
plugins declare typed settings rendered by the host (see below). Assign plugin tools and
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
`lifespan` (async context manager), `version` (package version), and `settings`.
`api_version=(1, 1)` declares the minimum API minor version required. The host
accepts the same major and a minor no higher than its own `API_VERSION`.
Invalid versions,
duplicate IDs, missing/cyclic dependencies, duplicate tools and exact API route
collisions fail startup. Routes must use `/<plugin-id>` as their first segment; core
route prefixes are reserved. API routers mount beneath
`/api/v1` with core's API-key authentication. Tools use normal injected
`ToolRuntime[ToolContext]`, rather than accepting actor identity from the model.

Use `tool_context(runtime)` for a small immutable identity view (actor ID/handle,
thread ID, run ID). Core's `RunContext` and `Services` are not plugin contracts.

`PluginHost` supplies the async SQLAlchemy session factory, explicit `engine`,
`dialect`, `database_name`, private
state directory, plugin configuration, process-local `state` dictionary, and
`publish(event, thread_id, data)`. Use `host_from_request(request, id)` in routes
and `host_from_runtime(runtime, id)` in tools. These are server-side facilities,
not capabilities to expose as unrestricted model arguments. Hosts are keyword-only
objects. Plugin definitions are loaded once and shared by tools, routes and
lifecycle. `GET /api/v1/plugins` lists enabled IDs, versions, tools and settings scopes.

Events must match `<plugin-id>.[a-z0-9_.]+`; the host rejects foreign prefixes and
newlines. New tools should use a plugin-specific prefix. Existing tool names remain
stable to preserve grants; the registry records their `plugin:<id>` source and
rejects duplicate names.

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
core models. Foreign keys to removable core identities must specify `ON DELETE
CASCADE` or `SET NULL` as appropriate, so a plugin cannot accidentally prevent bot
or thread deletion. Do not call create_all on plugin metadata.

Back up the application database, LangGraph checkpointer/store, the secret key,
and `PLUGIN_STATE_DIR`; plugins may keep
files there. PostgreSQL roles or external stores may need additional backups as
documented by their plugin. Removing a bot does not automatically erase its
plugin files; removal/retention policy belongs to the plugin.

## Host-managed settings

```python
from typing import Literal
from pydantic import BaseModel, Field, SecretStr
from openbot.plugin_api import PluginSettings

class GlobalSettings(BaseModel):
    enabled: bool = Field(False, json_schema_extra={"env": "EXAMPLE_ENABLED"})
    token: SecretStr | None = None

class AgentSettings(BaseModel):
    access: Literal["off", "ro", "rw"] = Field(
        "off", json_schema_extra={"capability": True})

# Add to Plugin(...):
settings = PluginSettings(global_=GlobalSettings, agent=AgentSettings, version=1)
# In a tool:
# policy = await host.settings.for_agent(tool_context(runtime).actor_id)
# config = await host.settings.global_settings()
```

Core stores one document per plugin/scope/subject, with a schema version and
revision. Global settings appear in Settings; agent settings appear in each saved
bot's editor. Fields marked `capability` are labelled as tool access. Forms support
booleans, numbers, strings, literal enums and JSON arrays/objects; server validation
is authoritative. Top-level `SecretStr` fields are encrypted with the host secret
box, masked on reads, and preserved when the mask is written back. Use nullable
secrets and send `null` to clear them. Nested secrets are not supported.

The authenticated endpoints are:

- `GET/PATCH/PUT /api/v1/plugins/<id>/settings`
- `GET/PATCH/PUT /api/v1/bots/<actor-id>/plugin-settings/<id>`

Writes take `{"expected_revision": 0, "value": {...}}`. PATCH merges; PUT replaces
and restores defaults for omitted fields. Stale writes return 409. Unknown fields
and invalid documents return 422 without exposing secret inputs. Agent IDs must
identify bots. Bot creation/update tools do not accept plugin settings.

Precedence is field environment variable > `PLUGIN_CONFIG[plugin-id]` > stored
value > schema default. Deployment overrides apply to global settings and appear
locked in the UI; writes to locked fields are rejected. These values are validated
along with the document. Plugins read effective settings per call; there is no
cache or automatic workflow dispatch. Writes publish `<id>.settings.updated`.

For schema changes, provide `migrate(scope, old_version, value) -> dict` and bump
`version`. Reads apply this pure transformation; the next successful write stores
the new schema version. An invalid or unmigratable document remains stored, is
reported in the settings UI, and raises on tool reads. Never substitute permissive
defaults after such an error. PUT can repair an invalid document. Disabled plugins
retain settings; deleting an agent cascades its settings. Settings are not copied
by bot exports or agent creation tools.

This is **context hygiene and ordinary tool policy**, not containment of hostile
agents. An unrestricted shell running as the service user can read credentials or
change databases directly. An API key authenticates API calls but does not isolate
same-user processes. Plugins also run as trusted code. Keep capability defaults off.

Install the host and plugins explicitly together; do not assume the PyPI package
called `openbot` is this project. It belongs to a different distribution.

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
  id: "example", apiVersion: [1, 1],
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
