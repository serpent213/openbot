import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { Link } from "react-router-dom";
import { Api } from "../api/client";
import type { Sandbox, SandboxCredential } from "../api/types";
import { notifySave, useSaveMutation } from "../lib/saveNotifications";
import { Badge, Button, Card, Dialog, ErrorText, Hint, Input, SectionTitle } from "./ui";

export default function SandboxSettings() {
  const boxes = useQuery({ queryKey: ["sandboxes"], queryFn: Api.listSandboxes, refetchInterval: 10000 });
  const credentials = useQuery({ queryKey: ["sandbox-credentials"], queryFn: Api.listSandboxCredentials });
  return <div className="space-y-4">
    <Card className="space-y-2"><SectionTitle>Sandboxes</SectionTitle>
      <Hint>Configured workers are registered automatically. Names can change; IDs, accounts, and thread bindings cannot. Active is allocation state, not a live worker-health check. Tool selection remains in each bot's settings.</Hint>
      <Hint>Memory sharing is off by default. Enabling it shares bot-global long-term memory for new runs; scratchpads stay sandbox-scoped.</Hint>
      <Link className="text-sm underline" to="/settings/credentials">Manage named credentials</Link>
      <ErrorText error={boxes.error ?? credentials.error} />
      {boxes.data && !boxes.data.enabled && <Hint>Sandbox execution is disabled. Legacy Host behavior is unchanged; there are no controller-home clearing controls.</Hint>}
    </Card>
    {boxes.data?.sandboxes.map((box) => <SandboxRow key={box.id} box={box} credentials={credentials.data} enabled={boxes.data.enabled} />)}
  </div>;
}

function SandboxRow({ box, credentials, enabled }: { box: Sandbox; credentials?: SandboxCredential[]; enabled: boolean }) {
  const qc = useQueryClient();
  const [name, setName] = useState(box.name);
  const [grants, setGrants] = useState<string[] | null>(null);
  const [clearMode, setClearMode] = useState<boolean | null>(null);
  const [confirmation, setConfirmation] = useState("");
  const [kept, setKept] = useState<{ names: string[]; count: number } | null>(null);
  const refresh = () => { qc.invalidateQueries({ queryKey: ["sandboxes"] }); qc.invalidateQueries({ queryKey: ["sandbox-credentials"] }); qc.invalidateQueries({ queryKey: ["threads"] }); };
  const save = useSaveMutation({ mutationFn: (body: { name?: string; share_global_memory?: boolean }) => Api.updateSandbox(box.id, body), onSuccess: refresh }, "Sandbox settings saved");
  const attach = useSaveMutation({ mutationFn: () => Api.setSandboxCredentials(box.id, grants ?? []), onSuccess: () => { setGrants(null); refresh(); } }, "Credential attachments saved");
  const clear = useMutation({ retry: false,
    mutationFn: () => Api.clearSandboxHome(box.id, { keep_dotfiles: clearMode === true, confirmation, generation: box.generation ?? 1 }),
    onSuccess: (result) => { setKept({ names: result.kept, count: result.kept_count ?? result.kept.length }); setClearMode(null); setConfirmation(""); refresh(); notifySave("Home cleared"); },
    onError: () => { refresh(); notifySave("Home cleanup not confirmed. Inspect sandbox status; do not retry blindly.", "error"); },
  });
  const available = enabled && box.configured !== false && box.state === "active";
  const attached = grants ?? credentials?.filter((row) => row.sandbox_ids.includes(box.id)).map((row) => row.id) ?? [];
  return <Card className="space-y-3">
    <div className="flex flex-wrap items-center gap-2"><SectionTitle>{box.name}</SectionTitle><Badge>{box.configured === false ? "unavailable" : box.state}</Badge>{box.active_run_id && <Badge>busy</Badge>}</div>
    <p className="break-all text-xs text-muted">Account/resource: {box.resource} · ID: {box.id} · generation: {box.generation ?? 1}</p>
    <div className="flex gap-2"><Input aria-label={`Name for ${box.resource}`} value={name} onChange={(e) => setName(e.target.value)} />
      <Button variant="secondary" disabled={save.isPending || !name.trim() || name === box.name} onClick={() => save.mutate({ name })}>Rename</Button></div>
    <label className="flex items-center gap-2 text-sm"><input type="checkbox" aria-label={`Share bot-global memory for ${box.name}`} checked={box.share_global_memory}
      disabled={save.isPending} onChange={(e) => save.mutate({ share_global_memory: e.target.checked })} />Share bot-global memory</label>
    <ErrorText error={save.error ?? attach.error} />
    <details><summary className="cursor-pointer text-sm">Credential attachments</summary>
      <Hint>Applies to future runs and all processes/bots using this sandbox. Saving requires an idle sandbox. Detaching does not erase files or copied values, or revoke provider tokens.</Hint>
      {credentials?.map((row) => <label key={row.id} className="flex items-center gap-2 py-1 text-sm"><input type="checkbox" aria-label={`Attach ${row.name} to ${box.name}`} checked={attached.includes(row.id)}
        disabled={!available || !!box.active_run_id || attach.isPending} onChange={(e) => setGrants(e.target.checked ? [...attached, row.id] : attached.filter((id) => id !== row.id))} />{row.name} ({row.kind}: {row.target})</label>)}
      <Button variant="secondary" disabled={!credentials || grants === null || !available || !!box.active_run_id || attach.isPending} onClick={() => attach.mutate()}>Save attachments</Button>
    </details>
    <details><summary className="cursor-pointer text-danger">Clear home</summary>
      <Hint>Stops current work and cancels queued/paused runs. Preserves attached mounts and their parent directories. Does not erase database memory, scratchpads, or conversations. No automatic retry after uncertain cleanup.</Hint>
      <div className="flex flex-wrap gap-2 pt-2">{[true, false].map((keep) => <Button key={String(keep)} variant="danger" disabled={!available || clear.isPending} onClick={() => { setClearMode(keep); setConfirmation(""); clear.reset(); }}>
        {keep ? "Clear home, keep dotfiles" : "Clear home, including dotfiles"}
      </Button>)}</div>
    </details>
    {kept && <Hint>Home cleared. Preserved entries/mounts ({kept.count}): {kept.names.join(", ") || "none"}{kept.count > kept.names.length ? ", …" : ""}. The workspace and temporary directory were recreated.</Hint>}
    {clearMode !== null && <Dialog title={clearMode ? "Clear home, keep dotfiles" : "Clear home, including dotfiles"} titleId={`clear-${box.id}`} onClose={() => { if (!clear.isPending) setClearMode(null); }}>
      <p className="text-sm text-danger">This deletes files in {box.name}'s home. {clearMode ? "All top-level hidden entries remain, including any logins, sessions, caches, and extensions stored there." : "Dotfiles and home-stored native logins are deleted too. There is no application-specific login preservation."}</p>
      <Hint>Active work will be cancelled. Provider tokens are not revoked and past external effects are not undone. Named credentials remain stored centrally and attached files will be provisioned again on the next run.</Hint>
      <Input aria-label="Confirm sandbox name" placeholder={`Type ${box.name} to confirm`} value={confirmation} onChange={(e) => setConfirmation(e.target.value)} />
      <ErrorText error={clear.error} />
      <Button variant="danger" disabled={clear.isPending || clear.isError || !available || confirmation !== box.name} onClick={() => clear.mutate()}>{clear.isPending ? "Clearing…" : "Confirm clear home"}</Button>
    </Dialog>}
  </Card>;
}
