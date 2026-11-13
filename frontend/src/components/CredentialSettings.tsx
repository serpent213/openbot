import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { Api } from "../api/client";
import type { CredentialInput, SandboxCredential } from "../api/types";
import { useSaveMutation } from "../lib/saveNotifications";
import { Button, Card, ErrorText, Field, Hint, Input, SectionTitle, Select, Textarea } from "./ui";

const empty: CredentialInput = { name: "", kind: "env", target: "", value: "" };

export default function CredentialSettings() {
  const qc = useQueryClient();
  const credentials = useQuery({ queryKey: ["sandbox-credentials"], queryFn: Api.listSandboxCredentials });
  const [draft, setDraft] = useState(empty);
  const create = useSaveMutation({ mutationFn: () => Api.createSandboxCredential(draft), onSuccess: () => {
    setDraft(empty); qc.invalidateQueries({ queryKey: ["sandbox-credentials"] });
  } }, "Credential saved");
  return <div className="space-y-5">
    <Card className="space-y-3">
      <SectionTitle>Sandbox credentials</SectionTitle>
      <Hint>Named secrets for sandbox shells and native agent apps. Attach them in Settings → Sandboxes. Plugin/API credentials remain in their existing plugin settings; these are not controller credentials.</Hint>
      <Hint>Values are write-only and encrypted in the database. Any process in a granted sandbox may copy or disclose them. Detaching does not revoke tokens or remove existing file copies. Native OAuth dotfiles are not discovered or imported.</Hint>
      <ErrorText error={credentials.error} />
      {credentials.data?.map((row) => <CredentialRow key={row.id} credential={row} />)}
    </Card>
    <Card className="space-y-3">
      <SectionTitle>Add credential</SectionTitle>
      <form className="space-y-3" onSubmit={(event) => { event.preventDefault(); create.mutate(); }}>
        <Field label="Name"><Input aria-label="Credential name" required value={draft.name} onChange={(e) => setDraft({ ...draft, name: e.target.value })} /></Field>
        <Field label="Delivery"><Select aria-label="Credential delivery" value={draft.kind} onChange={(e) => setDraft({ ...draft, kind: e.target.value as "env" | "file", target: "" })}>
          <option value="env">Environment variable</option><option value="file">File in private home</option>
        </Select></Field>
        <Field label={draft.kind === "env" ? "Environment variable" : "Path relative to home"} hint={draft.kind === "env" ? "For example EXA_API_KEY. Execution-control variables are reserved." : "For example .ssh/project_key. No absolute paths, symlinks, or attached mounts; files are written with mode 0600. Existing regular files at this path are replaced."}>
          <Input aria-label="Credential target" required value={draft.target} onChange={(e) => setDraft({ ...draft, target: e.target.value })} />
        </Field>
        <Field label="Secret value">{draft.kind === "env"
          ? <Input aria-label="Secret value" type="password" autoComplete="new-password" required value={draft.value} onChange={(e) => setDraft({ ...draft, value: e.target.value })} />
          : <Textarea aria-label="Secret value" autoComplete="off" required value={draft.value} onChange={(e) => setDraft({ ...draft, value: e.target.value })} />}</Field>
        <ErrorText error={create.error} /><Button type="submit" disabled={create.isPending}>Add credential</Button>
      </form>
    </Card>
  </div>;
}

function CredentialRow({ credential: row }: { credential: SandboxCredential }) {
  const qc = useQueryClient();
  const [name, setName] = useState(row.name);
  const [value, setValue] = useState("");
  const refresh = () => { setValue(""); qc.invalidateQueries({ queryKey: ["sandbox-credentials"] }); qc.invalidateQueries({ queryKey: ["sandboxes"] }); };
  const save = useSaveMutation({ mutationFn: () => Api.updateSandboxCredential(row.id, { name, ...(value ? { value } : {}) }), onSuccess: refresh }, "Credential updated");
  const remove = useSaveMutation({ mutationFn: () => Api.deleteSandboxCredential(row.id), onSuccess: refresh }, "Credential deleted");
  return <div className="space-y-2 border-t border-line pt-3">
    <p className="text-sm">{row.kind === "env" ? "Environment" : "Home file"}: <code>{row.target}</code> · attached to {row.sandbox_ids.length} sandbox(s)</p>
    <Input aria-label={`Name for ${row.name}`} value={name} onChange={(e) => setName(e.target.value)} />
    {row.kind === "env"
      ? <Input aria-label={`Replacement value for ${row.name}`} type="password" placeholder="Leave blank to keep the stored secret" autoComplete="new-password" value={value} onChange={(e) => setValue(e.target.value)} />
      : <Textarea aria-label={`Replacement value for ${row.name}`} placeholder="Leave blank to keep the stored secret" autoComplete="off" value={value} onChange={(e) => setValue(e.target.value)} />}
    <Hint>Rotation requires all attached sandboxes to be idle. Native CLI continuation is restarted when credential grants or values change.</Hint>
    <ErrorText error={save.error ?? remove.error} />
    <div className="flex gap-2"><Button variant="secondary" disabled={save.isPending || remove.isPending || (!value && name === row.name)} onClick={() => save.mutate()}>Save credential</Button>
      <Button variant="danger" disabled={row.sandbox_ids.length > 0 || save.isPending || remove.isPending} onClick={() => { if (confirm(`Delete stored credential “${row.name}”? This does not revoke it at its provider or erase sandbox copies.`)) remove.mutate(); }}>Delete credential</Button></div>
  </div>;
}
