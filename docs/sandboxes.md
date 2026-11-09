# Optional sandbox prototype

An opt-in Linux execution backend, **not a claim of completed deployment isolation**.
Legacy single-account mode remains the default. The trusted Python server owns model
calls, orchestration and persistence; selected workers execute agent-directed I/O.
No additional Python dependencies, broker, root helper or automatic deployment.

## Capability matrix

| Path | Bound runs |
| --- | --- |
| Shell, read/write/edit/patch/list/find/search files, HTTP/fetch | Existing built-in implementations executed in worker subprocesses; existing result formats, caps/spill files and requested shell timeouts retained |
| Parallel tool calls | Concurrent operations within one run, with separate connections, durable intents and cleanup groups; no new global tool semaphore |
| Coding CLI | Shared adapter machinery, including installed Pi; worker-side discovery, version/login probes, temporary prompts, native sessions and streamed events |
| Repository instructions / selected project skills | Discovered and read in the worker; never from the controller workspace/state directory |
| Long-term memory | Scoped to sandbox + bot by default, including retrieval, tools and reflection after internal **and CLI** runs; explicit operator opt-in to the existing bot-global namespace |
| Scratchpad | Sandbox + bot scope, independently of global-memory opt-in; no implicit import of the legacy scratchpad |
| History / recall / todos / questions | Trusted server tools with current-thread scope |
| Scheduling | Current-thread creation; scheduled-message listing limited to the bot in this sandbox |
| Bot roster / instructions / installed server plugins / operator UI | Trusted shared configuration, not a multi-tenant UI authorization boundary |
| Worker CLI user extensions/MCP | Governed by the selected CLI adapter/permission mode; execute inside its worker operation, not as server plugins |
| Unadapted server plugin/MCP tools, bot creation/profile mutation, hosted web tools | Explicitly refused; never silently executed on the server |
| Operator management GUI / public sandbox backend registration / Podman / recycling | Deferred |

Coding adapters must be installed/enabled in **both** controller and worker. The
worker reuses `CliAdapter`, parsing, timeout and turn/session behavior; it does not
start plugin routes, lifespans or arbitrary Openbot plugin tools. The Pi plugin
needs no sandbox-specific fork. Native CLI permission modes remain adapter-level
choices, not replacements for OS isolation. As in local mode, OpenBot tools are not
injected into native CLIs: CLI turns receive the scoped memory/scratchpad prompt
snapshot and scoped post-turn reflection, while using their own native tools.

One active run per bot and sandbox remains enforced. Sandbox admission precedes
the global execution permit: A1 running and A2 waiting for A do not prevent B1
from using an idle global permit in B. A waiting bound run may have a **queued**
Run row, but does not count as running or consume a global execution permit.
This remains a **single-controller** prototype.

## Outcomes, cancellation and cleanup

Each operation has a durable intent committed before dispatch. Stored intent fields
are identity, scope, operation and state, **not arguments/output**. RPC v2 uses one
operation per authenticated Unix connection and streams CLI events before its
terminal response. Frames are bounded at 32 MiB, matching the existing CLI event
limit; normal tool output uses the existing tool caps and worker-side spill files.
There is no blanket 120-second execution cap or 135-second transport deadline.

- `rejected`: a structured preflight rejection, such as a missing working directory,
  invalid tool arguments, absent worker CLI or unavailable namespace prerequisite.
  Nothing agent-directed was dispatched; fix the problem and submit a new request.
- `completed`: a terminal tool/CLI result was received **after operation cleanup**.
  This includes ordinary errors (for example `fetch_url ftp://…`, a missing file,
  invalid patch, nonzero exit or reported CLI timeout); it does not mean the tool
  succeeded. These errors leave the binding usable.
- `unknown`: transport loss, an unexpected worker crash, cancellation without a
  terminal result, or an otherwise unconfirmed outcome. The binding is quarantined.
  **Never automatically retry/replay** such a command. Cleanup cannot undo external
  effects and does not turn an unknown result into proof that nothing happened.

A separate control connection is held for the **whole run**. Finish or EOF (including
controller death) stops all remaining run operations. Each operation gets a cgroup;
`cgroup.kill` and the `populated` check complete before its terminal response.
Cancelling one connection stops that operation, not its parallel siblings. Whole-run
cleanup also kills remaining descendants and must be confirmed before releasing the
worker lease. Failure to confirm cleanup quarantines the core binding and keeps the
worker unavailable. Service death is additionally covered by systemd
`KillMode=control-group`. Interrupted startup state is quarantined, not replayed.

### Required kernel/systemd prerequisites — agree with the administrator first

The worker **refuses** execution without the following; there is no killpg-only or
server-execution fallback:

1. Unified cgroup v2 with `cgroup.kill` (Linux 5.14+), and the worker service's own
   delegated subtree (`Delegate=yes`). No root/systemd D-Bus helper is used.
2. Enabled unprivileged user and mount namespaces, including nested user namespaces.
   Container/LXC mappings, sysctls, LSM/seccomp and systemd namespace restrictions
   must permit the bootstrap. The module permits `user` and `mnt` namespaces.
3. A single cgroup-v2 mount at `/sys/fs/cgroup`. Before importing adapters/project
   data, a child joins its operation cgroup, creates a private user/mount namespace,
   bind-remounts cgroupfs read-only, and enters another user/mount namespace to lock
   that inherited flag. A remount-rw attempt must then be denied, or execution is
   refused. Commands therefore cannot move themselves into a sibling/root cgroup.
4. Supervisor proc access is made non-dumpable; private runtime directories and
   ordinary proc restrictions must prevent `/proc/<supervisor>/root/fd` bypasses.
   Separate operation user namespaces also prevent access to a sibling's writable
   supervisor namespace. Installed worker code/interpreter must not be worker-writable.

These mechanisms handle `setsid`/double-fork descendants, not just process groups,
**provided the prerequisites hold**. Validate them on the pinned kernel/systemd/LXC
configuration before admitting real workloads or credentials. Daemons/control-plane
services outside the unit (including Nix builders, Docker and other brokers) are
separate escape paths that site policy must deny or explicitly mediate.

## Configuration and operator API

Boot settings (not agent-editable):

- `SANDBOXES_ENABLED=true` requires `OPENBOT_API_KEY`.
- `SANDBOX_RESOURCES`: for example
  `{"openbot-sbx1":{"socket_path":"/run/openbot-sbx1/worker.sock","uid":62000}}`.
  Select collision-free stable UIDs; the example reserves nothing.
- `SANDBOX_DEFAULT_ID`: optional allocated sandbox used by ingress without an
  explicit selection. Missing bindings fail closed. `/new` inherits its binding.

The controller and workers require **distinct non-root UIDs**. Never put the
operator API key, controller DB/model keys or server configuration in worker homes,
environments or prompts. API inventory cannot create accounts or change host policy.

Authenticated endpoints:

- `GET /api/v1/sandboxes`: inventory/capabilities, not a live isolation certificate.
- `POST /api/v1/sandboxes`: `{"name":"research","resource":"openbot-sbx1"}`.
  IDs, resource specs/generation and thread bindings remain immutable. Allocated or
  retired UIDs/resources cannot be allocated again.
- `POST /api/v1/threads`: select `sandbox_id`, optionally a **relative**
  `working_directory`. Only the worker checks existence. Fixing a nonexistent
  directory does not require allocating another sandbox.
- `PATCH /api/v1/sandboxes/{id}`: `{"share_global_memory":true}` explicitly selects
  the existing bot-global memory namespace for **newly created runs**, for both
  reads and writes/reflection. Default is false. Each run snapshots the policy;
  paused/queued existing runs retain theirs. Changing it never copies/deletes data
  and does not undo knowledge already written to transcripts or worker files.
- `GET /api/v1/sandboxes/{id}/executions`: recent operation states.
- `GET /api/v1/providers/cli/{provider}?sandbox_id=…`: worker-side version/login
  probe. Login is queried through the CLI's own status command, never by the server
  opening authentication files. `logged_in=null` means the adapter has no probe.
- `GET /api/v1/tools/skills?sandbox_id=…` or
  `/api/v1/sandboxes/{id}/skills?directory=…`: worker project-skill discovery.
- `GET /api/v1/workspace/directories?sandbox_id=…&path=…`: worker directory browsing.
  Discovery/probes take a short exclusive lease; a busy sandbox returns an
  actionable conflict, not a local fallback. Global discovery in sandbox mode
  requires an explicit sandbox selection. The existing GUI does not yet select it.
- `POST /api/v1/sandboxes/{id}/retire`: quarantine and request active-run cancellation.
  This does not revoke network policy, erase credentials/data or make a slot reusable.
  Inspect cleanup confirmation and stop the unit before any manual retirement work.

Memory namespaces are `("sandboxes", sandbox_id, "bots", bot_id, "memories")` or,
when explicitly shared, the unchanged `("bots", bot_id, "memories")`. Reflection
batches include their namespace, including fallback-model reflection. CLI reflection
still requires a configured server-side chat provider, as in legacy mode. Scratchpads
have a separate `(sandbox_id, actor_id)` table and are **never** switched to the
legacy global scratchpad by the memory flag. Existing global memory/scratchpad UI
views remain global; scoped management UI is deferred.

CLI session events include sandbox ID, binding generation and memory namespace;
reuse also requires the same bot, thread, backend and worker-resolved cwd. A policy,
binding or directory mismatch starts a new session, not an imported one. An explicit
CLI-reported missing session can use the adapter's existing fresh-session behavior;
an uncertain RPC outcome cannot. Native session/login files persist in the worker
home. Threads sharing a UID/home are not hard confidentiality boundaries from each
other, regardless of logical session/memory keys.

Migrations `0025` and `0026` add bindings/intents, the memory opt-in/run snapshot and
scoped scratchpads. Legacy rows/data stay in their original namespace; old bound
runs without a memory snapshot default to sandbox scope. Back up the DB and homes
consistently; restoring one without reconciling the other is unsafe. Disabling the
feature never converts an already-bound thread into a local run. Stop workloads and
upgrade controller and workers together: RPC v1 and v2 are deliberately not mixed.

## Worker provisioning

Launch the installed package as the slot UID in its delegated service cgroup:

```sh
python -I -m openbot.sandboxes.worker \
  --resource openbot-sbx1 \
  --root /home/openbot-sandboxes/openbot-sbx1/workspace \
  --socket /run/openbot-sbx1/worker.sock \
  --controller-uid CONTROLLER_UID \
  --plugin openbot_pi \
  --cli-path pi=/absolute/worker/pi
```

The cgroup root is detected from `/proc/self/cgroup`; `--cgroup-root` can specify
it explicitly, but must contain this worker and be a delegated service subtree.
It is not the hierarchy root. Home must be worker-owned 0700; workspace must lie
inside it. Socket parent must be worker-owned and not group/world writable. Give
only the controller membership in the slot's private IPC group. Workers must not
share each other's groups or the controller UID. Existing sockets fail closed;
stop/reconcile the service instead of replacing a live endpoint. Root-created
socket activation is not used: `SO_PEERCRED` must identify the worker socket creator.

`nix/sandboxes.nix` remains opt-in and **unactivated**: default pool 16, explicit
stable UID/GID base, private homes/groups, delegated restricted units and resource
limits. `workerPlugins = [ "openbot_pi" ];` selects installed adapter modules;
`cliPaths.pi` optionally pins its worker executable. `python` must contain core,
adapter packages and their existing dependencies. `packages` controls worker PATH.
The module is syntax-checked only, not evaluated/deployed against the site's pinned
configuration. There is no safe pool shrink/renumber/recycle hook.

Child environments allow only PATH, locale and CA paths from the **worker service**,
then set its HOME/USER/LOGNAME and a private per-operation TMPDIR. Server environment,
DB/model/operator credentials, arbitrary preload/profile/proxy variables are not
forwarded. Extra worker-only extension keys need private native configuration or an
explicit worker-only CLI wrapper, not a copy of the controller environment. Project
devenv activation is an explicit worker command. Shells use non-login `bash -c`.
Existing HTTP implementations run with this clean environment. Output spill files remain in the worker workspace. CLI
temporary prompts are created in the worker (0600); the supervisor removes each
operation's temporary directory after cleanup, including cancellation.

Provision native CLI logins/worker-only keys explicitly as that sandbox user, with
the worker stopped. Do not run unrelated services/login sessions under a pool UID.
No login contents are copied/read by core, and no shared OAuth cache/refresh broker
is introduced. Removing login files does not revoke provider tokens. Future reuse
must stop all processes and remove/quarantine **all** writable credentials/sessions
and persistent state. No recycling/reactivation endpoint is supplied.

## Disposable administrator-assisted pilot

The operator owns infrastructure and live pilot execution. Before using real state:

1. Provision one controller and **two worker UIDs** with fake secrets and the
   cgroup/namespace prerequisites above. Validate delegation and the bootstrap's
   remount-rw denial. Stop at any missing prerequisite; do not relax it into fallback.
2. Review filesystem masks, proc access and all control sockets/Nix-store paths.
   Read-only files can still leak secrets. Deny Nix-daemon/build/fetch and other
   privileged intermediaries until their separate policy is understood.
3. Apply protected-service firewall policy, including IPv6, localhost-published
   ports and direct container addresses. The module provides no site firewall.
4. Check real UIDs, own-file access, other-home/server-secret denial, cross-worker
   RPC denial, ordinary internet access and authorized/unauthorized service access.
5. Run A1/A2/B1 admission and within-run shell barriers. Cancel one of two parallel
   operations. Try `setsid`, double forks and attempted cgroup migration; verify the
   affected operation is empty and its sibling survives. Check the run group is
   empty after normal completion, cancellation and control-connection loss.
6. Interrupt controller/worker and terminal replies; inspect durable intents,
   quarantine and absence of automatic replay. Exercise nonexistent cwd, bad URL,
   missing CLI and timeout errors; known errors must not consume pool slots.
7. With explicitly provisioned private native login state, run Pi twice in the same
   bound thread and verify streaming/continuation. Verify changed scope cannot
   reuse a foreign session. Check separate sandbox memory/scratchpads, explicit
   global-memory opt-in and CLI reflection. Do not expose production CA/service data.
8. Retire a slot and verify it cannot run or be allocated again. Quarantine data;
   cleanup/reconciliation and token revocation are deliberate operator work.

Automated tests use real sockets/subprocesses and fake CLIs under **one UID**, with
explicit test-only identity/namespace bypasses and **fake cgroups**. They test routing,
admission, concurrent operations, protocol outcomes, stream/session behavior, scoped
state and cleanup control flow; they do **not** prove actual cross-UID, kernel cgroup,
namespace, filesystem or network isolation. Pi adapter tests use the installed
sibling plugin with a fake executable, not a live provider login/continuation pilot.

Deferred: audited server plugin/MCP adapters, scoped management GUI, credential grant
store/broker, deployment reconciliation, automatic recycling/safe pool shrink,
backend-created resources and a public/Podman backend. Durable delivery cannot give
exactly-once external shell/HTTP effects; uncertain effects need inspection or
idempotency at the external system.
