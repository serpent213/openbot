# Installed browser app (PWA)

OpenBot can be installed from a supporting browser over **trusted HTTPS** (or a
browser-supported localhost development origin). It remains a server-backed app:
bots/runs do not depend on keeping the browser open. Mobile OSes can suspend the UI;
installation does not grant unrestricted background execution. Electron does not
register the service worker, and Vite development does not register it either.

## Identity and icons

`frontend/public/manifest.webmanifest` declares a stable same-origin ID/scope `/`,
launches `/inbox`, requests standalone display, and provides Inbox/Threads shortcuts.
No credentials belong in the manifest or launch URLs. A 180px PNG Apple touch icon,
192/512px PNGs and a 512px maskable icon use the existing hexagonal OpenBot mark.
`app-icon.svg` has an opaque square background and mask-safe artwork; the OS supplies
its own clipping. The browser favicon remains `favicon.svg`.

The committed PNGs need no build/runtime image dependency. To regenerate from the
repository root (librsvg is only an asset-generation tool):

```sh
devenv shell -- nix shell nixpkgs#librsvg --command bash -c '
  for size in 180 192 512; do
    name=icon-$size
    [ "$size" = 180 ] && name=apple-touch-icon
    rsvg-convert -w "$size" -h "$size" frontend/public/app-icon.svg -o frontend/public/$name.png
  done
  cp frontend/public/icon-512.png frontend/public/icon-maskable-512.png
'
```

iOS may retain the icon of an existing Home Screen installation. After deployment,
remove and re-add the Home Screen app if necessary. Removing it may also remove its
local browser storage: preserve drafts first and be prepared to enter the browser
API key again. This is unrelated to worker/native CLI login state.

## Sleep, connectivity and drafts

Network/server failures keep the current route rather than redirecting to Inbox.
The UI shows a connection banner, refreshes query state on return/online/BFCache
restore, and replaces potentially stale SSE connections. Reopening the stream
reconciles server state; SSE history is not assumed to be replayable. Mutations
execute immediately or fail, without an automatic offline send queue or retries.
A failed/uncertain send must be checked against the conversation before retrying.

Text drafts are saved by default in **plaintext localStorage**, scoped to the browser
origin, not to an account or sandbox. They are not encrypted or synced and may contain
sensitive text. Settings → Browser access can disable persistence (deleting saved
copies) or delete saved copies while leaving text already open in memory alone.
Use that control before sharing the browser/device. Storage is bounded to 20 drafts,
64,000 characters each, with seven-day expiry checked on subsequent storage access.
Images are never persisted. Oversized drafts or unavailable/full storage fall back
to memory; persistence is not guaranteed. Clearing browser storage clears saved drafts.
Only confirmed successful sends clear the matching revision; later edits survive.
A restored in-flight send is flagged as uncertain and is never replayed automatically.

The shell uses dynamic/visual viewport dimensions, keyboard-aware height, and safe
area padding. Mobile controls use at least 16px text to avoid Safari focus zoom;
pinch zoom remains enabled. Browser theme colours follow the selected app theme.

## Caching and updates

`frontend/pwa-build.ts` generates `dist/sw.js` from a content-derived version and an
exact list of public build assets. It precaches the public entry shell, install icons,
manifest, theme initializer and offline page. Other public JS/CSS/font chunks are
cached on demand. The 3D labs and their model files are not precached. Core pages
are route-lazy; a failed chunk load has a recoverable error screen, not an automatic
reload. A route that has never loaded may still be unavailable offline.

The worker **never intercepts** API/SSE paths, non-GET requests, foreign origins,
query-bearing URLs, or requests with API-key/Authorization headers. It never caches
conversation history, credential responses or mutations. Navigation is network-first
with a cached public-shell fallback on network/5xx failures; navigation responses
are not themselves cached. No background sync, push subscription or offline send
queue is added. Browser storage eviction can remove offline availability.

Public fingerprinted JS/CSS/fonts receive one-year immutable HTTP caching. HTML,
worker scripts, install metadata and other non-fingerprinted files revalidate. If a
reverse proxy overrides these headers, preserve that distinction. Do not apply an
SPA catch-all to missing scripts or API paths. Controller CSP continues to allow
only same-origin scripts/workers; no policy relaxation is needed.

Updates install in the background but do not call `skipWaiting` automatically.
An explicit **Reload to update** confirmation warns about unsaved forms/images and
non-persisted text. The approving window reloads after activation; other open windows
are offered a reload rather than forced to discard work. One previous build cache is
retained for older tabs; tabs spanning several deployments may need a manual reload.
Only OpenBot's own `openbot-shell-*` caches are cleaned. Failed installs leave the
previous working version alone. Resume checks for updates are throttled to once per
minute. There is no service worker registration in Electron or Vite dev.

## Verification and rollout

Automated tests cover icon dimensions/manifest links, resume reconciliation, route
preservation, storage expiry/limits/failures, uncertain sends, viewport/theme updates,
cache exclusion rules, failed installs, update consent, lazy-load failures, and static
HTTP caching. Build checks validate worker generation. These are not real iOS tests.

Before rollout acceptance, use a real iPhone/iPad and a Chromium installed app:

1. Install via trusted HTTPS; check icon, standalone launch, title and deep links.
2. Open a thread, type, background/lock the device, then resume after server activity.
3. Test airplane mode and recovery: keep the thread; reconcile messages without
   duplicate sends. Terminate/reopen the app and check text-draft restoration.
4. Check keyboard, rotation, notch/home indicator, pinch zoom, and light/dark override.
5. Deploy a second build while a draft and attachment are open. Decline the update,
   verify work remains, then accept deliberately. Test a second open window too.
6. Inspect Cache Storage: public UI assets only; no API/SSE or credential-bearing URLs.
7. Verify Electron launch and ordinary browser use remain unchanged.

No deployment, certificate changes, notification permission requests or device tests
are performed by the coding checks themselves.
