# Optional sandbox prototype: first execution slice

This is an opt-in, Linux-only prototype, **not completed isolation or a deployment
recipe that can be enabled without a site security review**. The default remains
the existing single-account mode. No extra Python dependencies or message broker.
The Python model/orchestration loop stays in the trusted server.

## What this slice supports

- Immutable logical sandbox IDs, immutable thread bindings and run snapshots.
- An explicit inventory of preprovisioned Linux worker sockets/UIDs. Allocation
  persists in the application database, including retired bindings. UID aliases
  cannot allocate the same identity twice. Names, binding specs and generations
  cannot be edited through the API. Resources are externally managed.
- One active run per sandbox; different sandboxes can run concurrently under the
  existing global concurrency limit. This is a **single-controller** prototype.
- Internal-model bots using `run_shell`, `read_file`, `write_file`, `edit_file`,
  `list_files`, `http_request`, `fetch_url`. Tools use fixed built-in schemas, not
  arbitrary plugin handlers selected by name. Execution occurs in worker-UID
  subprocesses, never the server. File-tool spills stay inside the worker context;
  shell/HTTP output is bounded without server-side spilling.
- Linux Unix sockets with peer-UID authentication in both directions, bounded
  length-prefixed JSON, one operation per connection, no automatic retries.
- Durable execution intents (IDs, scope, operation and state, **not arguments or
  output**), recorded before dispatch. Results are untrusted text. Unknown results
  quarantine the binding, including cancellation or loss of a response. A restart
  quarantines interrupted active bindings/pending intents rather than re-executing.
- Operator API listing, allocation, thread selection, execution inspection, and
  quarantine-on-retirement. There is no sandbox management UI yet.

### Explicitly unavailable in sandbox mode

Coding CLI turns (Pi, Codex, Claude), CLI probes/temp prompts/session reuse;
repository instruction/skill discovery; search/patch helpers; user/plugin/MCP
tools; bot creation/global profile mutation; scheduling tools; scratchpad and
long-term bot memory tools. A bot configured with unsupported tools, repository
instructions, skills, hosted web tools or a coding-CLI provider is refused before
prompt preparation. Remove those assignments explicitly for the pilot.

Memory retrieval and reflection are disabled for bound runs even if the bot's
legacy memory flag is on. No legacy memory or scratchpad is imported. The only
host tools retained are `list_bots`, `ask_human`, `read_history` and
`recall_messages` (the latter two use the current immutable thread). Bot roster,
bot instructions and the operator UI remain trusted shared configuration; this
is not a multi-tenant UI authorization model.

The directory picker returns an explicit unavailable error when sandbox mode is
on. Thread working directories must be relative to the worker's fixed workspace;
only the worker checks existence. `/new` inherits the current binding. Other
thread creation paths use an explicit sandbox ID or `SANDBOX_DEFAULT_ID`, otherwise
refuse creation. Existing unbound threads cannot run while sandbox mode is enabled.
Disabling sandbox mode never turns a previously bound thread into a local run.

## Configuration and operator API

Boot settings (not editable through the agent-facing runtime settings):

- `SANDBOXES_ENABLED=true` requires a nonempty `OPENBOT_API_KEY`.
- `SANDBOX_RESOURCES` is a JSON object, for example:
  `{"openbot-sbx1":{"socket_path":"/run/openbot-sbx1/worker.sock","uid":62000}}`.
  Select collision-free UIDs for the actual host; the example is not a reservation.
- `SANDBOX_DEFAULT_ID` is optional and must refer to an allocated sandbox.

Use the authenticated operator UI/API connection; never put the operator API key
in a worker home, environment, command, or model prompt. The controller and workers
must have **distinct non-root UIDs**. API inventory cannot create accounts, change
permissions, start units, or alter firewall rules.

1. `GET /api/v1/sandboxes` lists capacity and supported capabilities.
2. `POST /api/v1/sandboxes` with `{"name":"research","resource":"openbot-sbx1"}`
   allocates an inventory resource. Allocation is not a worker health guarantee;
   a worker probe runs before each bound agent run. Duplicate names/resources/UIDs
   return conflict, including retired resources.
3. `POST /api/v1/threads` with `sandbox_id` and optional relative
   `working_directory` creates a bound thread. There is no rebinding endpoint.
4. `GET /api/v1/sandboxes/{id}/executions` lists the latest operation outcomes.
5. `POST /api/v1/sandboxes/{id}/retire` quarantines the resource and requests
   cancellation of the active run. It does **not** certify that all descendants
   stopped, revoke network policy, destroy data, or make the slot reusable.

Database migration `0025` adds optional scope fields plus sandbox/intent tables.
Legacy data is left in its original namespace. Back up the application database
and sandbox homes consistently. Replacing/restoring a DB without reconciling worker
homes and bindings is unsafe. Renaming/remapping inventory entries is not a way to
recycle an account; reconcile deliberately before changing a binding's resources.

## Worker and provisioning

Launch the installed host Python package as the slot UID:

```
python -m openbot.sandboxes.worker \
  --resource openbot-sbx1 \
  --root /home/openbot-sandboxes/openbot-sbx1/workspace \
  --socket /run/openbot-sbx1/worker.sock \
  --controller-uid CONTROLLER_UID
```

The home must be worker-owned, mode 0700, and the workspace must be inside it.
The socket parent must be worker-owned and not group/world writable. Give only
the controller membership in that slot's private group so it can traverse the
0750 runtime directory and connect. Do not give workers each other's groups.
The worker creates its own socket: root-created socket activation would cause
client `SO_PEERCRED` to identify the socket creator, not the worker. Existing
socket paths fail closed; stop/reconcile the unit instead of blindly replacing
an endpoint. A dedicated systemd RuntimeDirectory removes the socket on unit stop.

`nix/sandboxes.nix` is an **unactivated, opt-in provisioning module** with a default
pool of 16, explicit stable UID/GID base, private groups/homes, restricted units and
resource limits. It requires an existing controller account/stable UID and a Python
executable containing Openbot's existing dependencies. No `nixos switch` is invoked
by Openbot. The module has been syntax-checked, not evaluated/deployed against the
operator's pinned infrastructure. Shrink/renumber/rollback safety is not implemented:
do not shrink or renumber an allocated pool. Account deletion does not erase files
or prove processes stopped.

The worker builds an explicit subprocess environment from PATH, locale and CA paths,
then sets its own HOME/USER/LOGNAME. It does not load server `.env` settings or pass
the server environment, DB credentials, encryption keys, API keys or arbitrary
library/preload/profile variables. Shells use `bash -c`, not login profiles.
Project environment/devenv activation is an explicit worker-side command.
HTTP ignores environment proxy settings and limits response bytes. Shell output
is drained with bounded retention; requests are capped at 256 KiB and operation
execution at 120 seconds (a lower shell timeout is honored). Long builds and coding
CLI event streams need a later streaming protocol.

## Required administrator-assisted pilot

Do this on a disposable host/VM/LXC with fake secrets, not existing production state:

1. Provision a controller and **two different worker UIDs**, private homes/runtime
   directories, and worker units using the installed package. Confirm controller
   IPC groups and that workers have no sudo, Docker/control-plane sockets, broad
   supplementary groups, or Nix trusted-user membership.
2. Review unit filesystem restrictions against the deployment's pinned systemd/NixOS.
   Mask server DB/key/config files, other homes, process environments and sensitive
   Nix-store paths. Read-only mounts do not hide secrets. Protect `/run` control
   sockets. Unit cgroups, not ordinary process groups, are the retirement boundary.
3. Supply explicit protected-service firewall policy (including IPv6, localhost
   published ports and direct container addresses). Test Nix-daemon/build/fetch,
   proxy and host-service intermediaries before allowing them. **The generic module
   deliberately does not implement a site firewall or audit Nix-store secrets.**
4. Start a disposable Openbot database with API authentication and the two inventory
   entries. Allocate both, create bound threads, and use an internal-model pilot bot
   with only supported tools, no skills/repository instructions/scratchpad. A fake
   model can be used first; real model credentials remain in the controller only.
5. Verify actual execution UIDs, own-file writes, other-home/server-secret denial,
   authorized/unauthorized protected service access, ordinary internet access,
   unauthenticated API denial and rejection of cross-worker RPC peers.
6. Kill the controller/worker mid-command; verify intent state, quarantine and no
   automatic replay. Cancel normally and check process cleanup. Try descendants
   using `setsid`: ordinary group cancellation is not a cgroup-wide guarantee.
   Stop the worker unit and verify its cgroup is empty before any cleanup.
7. Retire a sandbox; verify it cannot run or be allocated again. Preserve its data
   behind a trusted quarantine boundary. No automated cleanup/recycle/reactivation
   endpoint is supplied; a failure currently consumes that slot until a reviewed
   operator recovery procedure exists.

The unprivileged test suite uses real sockets and disposable subprocesses under one
UID, with an explicit test-only bypass of the distinct-UID controller check. It tests
routing, authentication comparisons, lifecycle and legacy regressions; it does **not**
prove filesystem/network separation across Unix identities.

## Credentials and future work

When coding CLI routing is added, each CLI's native login state must live in its
private sandbox home and persist between runs. Pi native OpenAI login and a separately
provisioned Exa key fit that model. Shared OAuth caches/central refresh remain deferred.
Before recycling, stop all processes and remove/quarantine the entire writable state,
including copied credentials, sessions and temporary files. Deleting login files does
not revoke provider tokens. No native authentication is needed by this internal-loop
pilot, and no CLI authentication files are read by the implementation.

A future named credential store/grant evaluator belongs in core. Thread-subset grants
cannot isolate credentials already readable by threads sharing the same persistent
UID/home. Use separate sandboxes for a hard boundary; do not call prompt hygiene an OS
security boundary.

The backend contract is internal and intentionally Linux-specific for this first
implementation. Core owns identity, authorization, durable intent and scheduling;
worker transport owns execution. Backend-created resources, public plugin registration,
Podman, CLI streaming/session scoping, safely adapted host plugins, UI, automatic cleanup,
reactivation and safe pool shrink remain unimplemented. Durable delivery cannot provide
exactly-once shell side effects; uncertain effects require inspection/idempotency at the
external system. No NATS/RabbitMQ or general root helper is involved.
