import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { Api, api } from "../api/client";
import { Button, Card, ErrorText, Field, Hint, Input, SectionTitle, Select, Textarea } from "./ui";

type Property = { title?: string; description?: string; type?: string; enum?: string[]; default?: unknown; minimum?: number; maximum?: number; writeOnly?: boolean; capability?: boolean; anyOf?: Property[] };
type Document = { revision: number; value: Record<string, unknown> | null; locked: string[]; error: string | null; schema: { properties: Record<string, Property> } };

export default function PluginSettings({ actorId }: { actorId?: string }) {
  const plugins = useQuery({ queryKey: ["plugins"], queryFn: Api.listPlugins });
  const scope = actorId ? "agent" : "global";
  return <>
    <ErrorText error={plugins.error} />
    {plugins.data?.filter(p => p.settings_scopes.includes(scope)).map(p =>
      <SettingsCard key={`${p.id}:${actorId ?? "global"}`} plugin={p.id} actorId={actorId} />)}
  </>;
}

function SettingsCard({ plugin, actorId }: { plugin: string; actorId?: string }) {
  const path = actorId ? `/bots/${actorId}/plugin-settings/${plugin}` : `/plugins/${plugin}/settings`;
  const query = useQuery({ queryKey: ["plugin-settings", path], queryFn: () => api<Document>(path) });
  if (!query.data) return <ErrorText error={query.error} />;
  return <SettingsForm key={query.data.revision} plugin={plugin} path={path} document={query.data} />;
}

function SettingsForm({ plugin, path, document: doc }: { plugin: string; path: string; document: Document }) {
  const qc = useQueryClient();
  const [draft, setDraft] = useState<Record<string, unknown>>({});
  const [parseError, setParseError] = useState<Error | null>(null);
  const [saved, setSaved] = useState(false);
  const save = useMutation({
    mutationFn: () => api<Document>(path, { method: doc.error ? "PUT" : "PATCH", json: { expected_revision: doc.revision, value: draft } }),
    onSuccess: result => { qc.setQueryData(["plugin-settings", path], result); setDraft({}); setSaved(true); },
  });
  function change(name: string, value: unknown) { setDraft(d => ({ ...d, [name]: value })); setSaved(false); }
  return <Card className="space-y-3">
    <SectionTitle>Plugin: {plugin}</SectionTitle>
    {doc.error && <p role="alert" className="text-sm text-danger">{doc.error}</p>}
    {Object.entries(doc.schema.properties).map(([name, raw]) => {
      const prop = { ...raw, ...(raw.anyOf?.find(p => p.type !== "null") ?? {}) };
      const nullable = raw.anyOf?.some(p => p.type === "null");
      const value = name in draft ? draft[name] : doc.value?.[name] ?? raw.default ?? "";
      const locked = doc.locked.includes(name);
      const label = raw.title ?? name;
      return <Field key={name} label={`${label}${raw.capability ? " (tool access)" : ""}`}>
        {prop.enum ? <Select aria-label={label} disabled={locked} value={String(value)} onChange={e => change(name, e.target.value)}>
          {prop.enum.map(v => <option key={v} value={v}>{v}</option>)}
        </Select> : prop.type === "boolean" ?
          <input aria-label={label} type="checkbox" disabled={locked} checked={value === true} onChange={e => change(name, e.target.checked)} /> :
          prop.type === "array" || prop.type === "object" ?
            <Textarea aria-label={label} disabled={locked} defaultValue={JSON.stringify(value, null, 2)} onChange={e => {
              try { change(name, JSON.parse(e.target.value)); setParseError(null); }
              catch { setParseError(new Error(`${label}: enter valid JSON`)); }
            }} /> :
            <Input aria-label={label} disabled={locked} type={prop.writeOnly ? "password" : ["number", "integer"].includes(prop.type ?? "") ? "number" : "text"}
              min={prop.minimum} max={prop.maximum} step={prop.type === "integer" ? 1 : "any"} value={String(value ?? "")}
              onChange={e => change(name, nullable && e.target.value === "" ? null : ["number", "integer"].includes(prop.type ?? "") ? Number(e.target.value) : e.target.value)} />}
        {raw.description && <Hint>{raw.description}</Hint>}
        {locked && <Hint>Locked by deployment</Hint>}
      </Field>;
    })}
    <ErrorText error={parseError ?? save.error} />
    <div className="flex gap-2">
      <Button type="button" disabled={save.isPending || !!parseError || (!doc.error && !Object.keys(draft).length)} onClick={() => save.mutate()}>{doc.error ? "Replace settings" : "Save plugin settings"}</Button>
      <Button type="button" variant="secondary" onClick={() => { setDraft({}); setParseError(null); save.reset(); qc.invalidateQueries({ queryKey: ["plugin-settings", path] }); }}>Reload</Button>
      {saved && <Hint>Saved</Hint>}
    </div>
  </Card>;
}
