# Optional sandboxes: design and implementation hand-off

Date: 2026-11-08

Status: implementation authorized; trusted-server/worker split confirmed. A first
opt-in execution slice is implemented on `feat/sandboxes`, without deployment or
live account/service/firewall changes. This is not completed isolation. See
[the prototype capability matrix and pilot checklist](docs/sandboxes.md).

Current slice: durable sandbox/thread/run bindings, preprovisioned Linux worker
inventory, authenticated Unix-socket execution for selected internal-agent tools,
execution intents and quarantine without recycling. Coding CLIs, plugin/MCP tools,
repository/skill discovery, global memory/scratchpad and directory browsing are
unavailable in bound runs. The operator API exists; sandbox GUI and site network
policy do not. Cross-UID/systemd isolation still requires an administrator-assisted
pilot with fake secrets. Unprivileged tests do not establish an OS security boundary.

## Objective and agreed boundaries

Keep Openbot lightweight and usable in its current single-account mode. Add
optional named **sandboxes** so one server and UI can coordinate work with separate
filesystem identities and selected internal-service permissions.

The immediate concern is prompt injection causing an otherwise useful agent to
read unrelated credentials or operate another project's services. This is not a
hostile multi-tenant hosting platform. The operator, server implementation and
installed server plugins remain trusted. OS isolation limits tool execution;
it cannot make malicious instructions disappear or prevent misuse of deliberately
granted access. Kernel/container escapes are outside the prototype's guarantees.

Agreed choices:

- Preserve single-account execution when sandboxing is disabled.
- A thread selects a sandbox. Agents invoked in that thread inherit it.
- Multiple threads may intentionally share a sandbox and its Unix identity.
- Sandbox homes, checkouts, credentials and assignments persist between runs.
- Start with a Linux backend using separate Unix users and systemd workers.
- NixOS preprovisions a configurable pool, default 16 accounts.
- Support both externally provisioned resources and backend-created resources in
  the contract. Account creation on demand is not required in the first backend.
- Keep ordinary internet access available initially. Restrict selected protected
  local services instead of maintaining general internet domain allowlists.
- Put sandbox semantics and mandatory execution routing in core. Keep backend
  implementations replaceable and suitable for explicit plugin registration.
- Use a small Podman implementation later to test whether the contract is portable.
- Add third-party dependencies only where there is no reasonable alternative.
- Use devenv for local development and checks. Keep changes generic/upstreamable.

## Current code and deployment

Implementation started from core `aaaf4ae` in a clean cloned checkout; no untracked
`report.md` existed here. The older hand-off's `46522cc`/`report.md` description was
stale. Recheck repository state and preserve unrelated work before editing.

Legacy local mode has a global `WORKSPACE_ROOT` and a per-thread
`working_directory`, not a workspace security boundary. Absolute and `~/` thread
paths are accepted there. File tools perform path checks, but shell commands can
access anything their service account can access. Bound threads instead accept
only relative paths checked in the worker; unsupported operations fail closed.

Relevant entry points:

- `backend/openbot/runtime/runner.py`: internal agent loop and tool execution.
- `backend/openbot/tools/context.py`: execution context.
- `backend/openbot/tools/builtin/shell.py`: shell subprocesses; normally inherit
  the server environment and run a login shell. `SHELL_USER` requires a root
  server and affects only this path; it is not the proposed architecture.
- `backend/openbot/tools/builtin/files.py`, `search.py`, `workspace.py`: file
  access, subprocess helpers, path handling and directory browsing.
- `backend/openbot/tools/builtin/http.py`: HTTP currently runs inside the server.
- `backend/openbot/runtime/cli_agents/{base,run}.py`: CLI launches, environment
  filtering, temporary prompt files and session reuse.
- `backend/openbot/runtime/memory.py`: bot memory currently keyed by bot identity.
- `backend/openbot/plugin_api.py`: trusted in-process plugin API 1.3, currently
  exposing tools, coding backends, commands, routes and lifecycle hooks.
- `backend/openbot/api/{threads,bots,workspace}.py`: configuration and UI-facing APIs.

Two instances currently run in separate LXCs. One is the general Openbot instance
under Unix user `agent`; the other is on the host named `hermes`, under user
`openbot`. Operator update: Codex is currently disabled on the general instance;
native Pi is available there. Other deployment details below are historical and
were not reverified during implementation. No consolidation is part of this work.

ReactorCA's existing checkout is `/home/hermes/projects/reactor-ca` on the Hermes
host, owned by `hermes`, with its own Go-enabled devenv and an old Hermes profile.
The Openbot service hides that home. Use a new sandbox-owned development checkout
for the pilot; do not expose production CA state or deployment credentials.

Infrastructure configuration lives in the sibling infrastructure checkout:
`nix/machines/bucket/{openbot,hermes}/nixos/` and `nix/pkgs/openbot/`.
Read that repository's instructions before changing it. Some Nix sources contain
an intentionally restricted source-download token: do not dump it in output or
assume all Nix-store contents are free of secrets.

## Where the Python agent loop runs

Confirmed direction for the prototype: keep the internal Python agent loop in
the unprivileged, trusted Openbot server. Run agent-directed shell, filesystem,
HTTP and coding-CLI operations in the selected sandbox worker.

```text
Operator UI / chat channels
          |
Openbot server UID
  conversations, model requests, memory, scoped host tools, credentials
          |
  fixed sandbox assignment + execution requests
          |
Sandbox worker UID
  shell, file operations, HTTP, coding CLI and its subprocesses
          |
private home / checkout / permitted network services
```

The internal loop interprets model responses; it must not evaluate arbitrary
model-supplied Python inside the server. Every escape into executable code or
host access must go through the worker or an explicitly scoped trusted tool.
Audit subprocess creation, file reads/writes, output spilling, directory browsing,
MCP calls and plugin tools, not just `run_shell`.

Running the entire Python loop under the sandbox UID is possible, but merely
copying the server configuration there would expose model keys, database access
and encryption keys to that sandbox's shell. A useful version would require
narrow model/persistence/memory APIs, authenticated for a particular sandbox and
run, rather than giving workers the general Openbot API key or database URL.
That is a larger process split; do not make it a prerequisite for the prototype.

Coding CLIs necessarily run their own agent loops inside the worker. For the
prototype, each CLI keeps its native login dotfiles in the sandbox's private home,
provisioned explicitly when needed and retained between runs. Do not share OAuth
caches or implement a central refresh broker yet. Recycling a sandbox must remove
these login files before its pool account can be assigned again; a new sandbox
starts unauthenticated. Removing local files does not revoke provider-side tokens.
Credentials readable by that UID can potentially also be read by its commands:
document this limitation, use scoped accounts where possible, and never silently
share the server home.

## Core and backend contract

Core owns sandbox identity, thread assignment, GUI/API, authorization, scheduling
and routing. It must refuse a run when the selected sandbox cannot satisfy its
requirements; it must never switch that run to local execution automatically.

A sandbox has an immutable ID, an operator-facing name, backend/resource binding
and observed lifecycle state. A backend resource (Unix account or container) is
not interchangeable with the logical sandbox identity. Persist their assignment
across application restarts.

Backend responsibilities, with exact method names intentionally unsettled:

1. Describe capabilities and discover preprovisioned resources/capacity.
2. Allocate an existing resource, or create one when supported and permitted.
3. Execute processes with bounded streams, cancellation and exit status.
4. Perform file operations in the same isolation context.
5. Stop execution and report resource health.
6. Retire a binding; separately support explicit cleanup/destruction where owned.

Use structured requests, never a generic root shell interface. Distinguish
externally managed resources from backend-owned ones. Externally managed account
records must not be deleted by Openbot. Unsupported capabilities should be visible
in the UI and rejected by the API.

Implement the first concrete backend before freezing a public extension API.
Expose one sandbox-backend registration point rather than many lifecycle and
tool-interception hooks. Plugins are explicitly installed/enabled, never fetched
or activated by agents. A later Podman backend should use container handles, not
be forced to pretend its resources are host Unix accounts.

## Nix build-user precedent

Sources inspected during design:

- [NixOS account generation](https://github.com/NixOS/nixpkgs/blob/master/nixos/modules/config/nix.nix):
  `makeNixBuildUser` generates `nixbld1..N`, with UID base plus index and count
  controlled by `nix.nrBuildUsers`.
- [Nix runtime allocation](https://github.com/NixOS/nix/blob/master/src/libstore/unix/user-lock.cc):
  `SimpleUserLock::acquire` enumerates the configured group's members, tries a
  nonblocking exclusive lock on a per-UID file, and returns an available identity.
- [NixOS account reconciliation](https://github.com/NixOS/nixpkgs/blob/master/nixos/modules/config/update-users-groups.pl):
  tracks declared users and previous UID/GID assignments; omitted declarative
  accounts are removed even when mutable users are enabled. Homes are not deleted.
- [NixOS user management module](https://github.com/NixOS/nixpkgs/blob/master/nixos/modules/config/users-groups.nix):
  selects account-management machinery and validates configured identities.

These links track upstream; check the deployment's pinned nixpkgs and selected
account-management backend before implementation. Reuse NixOS's declarative
account machinery, not a private copy of its passwd/group editing code.

The opt-in provisioning module is now `nix/sandboxes.nix`. This fragment shows only
its common options; explicit UID base, controller UID and installed Python path are
also required. It has not been deployed or validated against site infrastructure:

```nix
services.openbot.sandboxes = {
  enable = true;
  poolSize = 16;
  userPrefix = "openbot-sbx";
  homeRoot = "/home/openbot-sandboxes";
};
```

Generate `openbot-sbx1..16`, private primary groups, stable UID/GID assignments,
locked login accounts, private homes and worker units. Select a collision-free
UID/GID range compatible with the LXC mapping. Do not reuse Nix build users or
give sandbox accounts sudo, Docker access, broad supplementary groups or Nix
trusted-user status. Ordinary untrusted Nix-daemon access may be needed for devenv.

The home parent is root-owned, with each child home owned by its sandbox account
and mode 0700. A configurable shared parent is not a shared permission boundary.
Protect server state under its separate identity. Unix permissions alone do not
hide world-readable host files; add appropriate worker filesystem restrictions.

`users.mutableUsers = true` is not necessary for this pool: Nix owns all account
records, while Openbot allocates existing resources. Do not change this global
option merely to enable sandboxing. Keep an independently provisioned pool
supported through a read-only resource inventory.

## Allocation, retirement and pool changes

The persistent sandbox-to-resource mapping differs from Nix's per-build lease.
Sixteen accounts initially support sixteen allocated sandboxes, not merely sixteen
concurrent commands. Several threads may share one sandbox intentionally.

Borrow exclusive per-resource locking for concurrency, but keep assignments in
durable state. On restart reconcile workers, assignments and incomplete operations
before offering resources as free. A released OS lock is not evidence of cleanup.
Unexpected or inconsistent state leaves a resource unavailable until reconciled.

Never return a resource to the pool immediately after a turn. Retirement must
stop all its processes, revoke resource-specific network access, and quarantine
or explicitly clean persistent state before reassignment. Preserve data by
default; deletion is an operator action. Cleanup must handle untrusted filesystem
contents safely and must not follow sandbox-controlled paths into the host.
Avoid immediate UID reuse while files or grants belonging to the old sandbox
remain reachable. Backups retain the logical sandbox identity, not just a UID.

Growing the pool adds accounts without renumbering existing ones. Shrinking must
be preceded by draining affected slots and a pre-removal check; deleting an account
does not stop its processes or remove files. The module/activation design must
address this before offering safe pool shrink, including rollback behaviour.

For the prototype, preprovision worker services and fixed policy bindings. GUI
allocation can then occur without root account management. If dynamic policy
changes or account creation are added later, use a separate narrow broker limited
to approved resources and policy names. Do not expose unrestricted systemd D-Bus,
sudo, firewall command strings or application-generated NixOS activation.

## Files, secrets and network permissions

- Use explicit subprocess environment allowlists, retaining runtime and CA-bundle
  paths. Do not automatically source personal/login profiles. Project devenv setup
  is explicit and runs under the sandbox identity.
- Keep server API keys, model credentials, database files and encryption keys out
  of worker homes, environments, inherited descriptors and visible process state.
- Private worker sockets must authenticate the server identity. A worker cannot
  impersonate the controller or submit jobs to another sandbox. Treat all worker
  output as untrusted results, not control-plane instructions.
- Run built-in HTTP operations under the sandbox UID so user-based firewall rules
  apply. Keep ordinary networking available. Protect named internal services,
  including published localhost ports and direct container-address paths.
- Network policies belong to the administrator. A generic sandbox must not be
  assigned a slot with sensitive grants without explicit operator authorization.
  Reassignment must not inherit a former sandbox's privileges.
- Audit runtime Nix-store visibility rather than mounting the whole store on the
  assumption that it contains no confidential data. Read-only does not mean secret.
- Keep Openbot's local administrative API authenticated. Sandbox tools must not
  acquire its operator credential or approve their own access changes.

## Memory, plugins and delegation

The same bot may work in different sandboxes. Scope automatic memory retrieval,
reflection, scratchpad and CLI-session reuse by sandbox as well as bot/thread;
preserve legacy namespaces for local mode without implicitly importing them. The
first slice disables these features in bound runs instead of partially scoping
them. It also refuses scheduling tools, global profile mutation and unadapted
plugin/MCP tools. These refusals apply before model/prompt preparation.

Keep the thread's sandbox fixed while jobs run. A sandbox change must not resume
an old CLI session or automatically carry private state across the boundary.
Start with an explicit new-thread workflow rather than inventing live migration.

Delegation within a sandbox inherits the current binding. It must not select a
more privileged sandbox or unsandboxed mode through model-controlled arguments.
Cross-sandbox communication can be explicit, with the receiving side's permissions
unchanged; it is information sharing, not immunity to prompt injection.

Installed Python plugins still execute with server privileges. Classify their
tools as worker-executed or explicitly scoped host operations. Unadapted tools
must be unavailable in sandbox mode, rather than quietly run as the server.
Review MCP transports and SQL/global-memory access similarly. Do not advertise
the existing per-agent SQL namespaces as an OS security boundary. Operator-enabled
global sharing is an intentional exception that should be visible in configuration.

## Implementation sequence and acceptance checks

1. Implemented first slice: durable records, immutable thread bindings, run snapshots,
   authenticated Linux worker RPC, allowlisted internal-agent execution and durable
   intent/quarantine. Legacy single-account mode remains available when disabled.
2. Before any bound run, unsupported state/execution paths are explicitly refused or
   disabled. Do not postpone these guards until after enabling workers.
3. Next: validate actual Unix identities, systemd cgroups, filesystem masks, protected
   internal services and Nix-mediated paths in disposable infrastructure with fake
   secrets. The Nix pool defaults to 16 but deployment/activation is not authorized.
4. Add coding-CLI worker execution, native sandbox-home login/session lifecycle and
   safely scoped memory/scratchpad and required plugins as separate reviewed slices.
5. Add the operator GUI and safe cleanup/reconciliation procedures. Retirement now
   quarantines without reuse; safe pool shrink, automatic deletion and recycling are
   unavailable. No Podman or backend-created accounts are implemented.
6. Later validate a minimal Podman backend before freezing a public backend plugin
   interface. Do not add a message broker as a prerequisite.

Focused checks, not exhaustive security certification:

- Existing local-mode behaviour remains usable.
- Both sandboxes can write their own files and use their intended development tools.
- Shell/file/HTTP/CLI operations use the correct identity; the other home and server
  secret files/process environment are inaccessible.
- Protected-service connections fail for an unauthorized sandbox and succeed for
  the authorized one, including direct internal-address attempts.
- Concurrent allocation cannot assign one resource to two unrelated sandboxes.
- Restart retains assignments; exhaustion or missing isolation fails clearly.
- Retirement stops descendants and does not expose old data or permissions on reuse.
- Agent-created bots, delegation, plugin/MCP tools and administrative API calls do
  not bypass the sandbox binding. Memory and CLI-session reuse stay scoped.

## Instructions for the next session

Start in this repository and read this document plus repository instructions.
The loop/worker split and first-slice implementation are authorized. The user
prefers minimal dependencies and surgical patches. There is no authorization to
consolidate containers, deploy, change live accounts/firewalls or move production
data. See `docs/sandboxes.md` for supported paths and exact remaining pilot work.
Worker environments are allowlisted; legacy local execution is intentionally unchanged.

Use the sibling infrastructure repository for Nix packaging/deployment when ready,
after reading its `nix/CLAUDE.md` and `teralink/CLAUDE.md`. Do not print credential
files or raw environments. Preserve any pre-existing reports and unrelated work.
Do not run integration tests that send chat messages or perform external actions
without authorization. Keep the core and this plan domain-neutral and upstreamable.
