import { useState } from "react";
import type { FormEvent } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Api } from "../api/client";
import type { RecurringSchedule } from "../api/client";
import { describeCron, presetCron, presets, weekdays, type Preset } from "../lib/cronPresets";
import { Badge, Button, Card, EmptyState, ErrorText, Field, Hint, Input, PageTitle, SectionTitle, Select, Textarea } from "../components/ui";

const browserZone = (() => { try { return Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC"; } catch { return "UTC"; } })();
const when = (iso: string | null, timeZone?: string) => iso
  ? new Date(iso).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short", ...(timeZone ? { timeZone } : {}) })
  : "—";

export default function ScheduledPage() {
  const qc = useQueryClient();
  const threads = useQuery({ queryKey: ["threads"], queryFn: Api.listThreads });
  const actors = useQuery({ queryKey: ["actors"], queryFn: Api.listActors });
  const threadTitle = (id: string) => { const t = threads.data?.find(t => t.id === id); return t ? t.title || t.id : id; };
  const handle = (id: string) => actors.data?.find(a => a.id === id)?.handle ?? "unknown";
  return <main className="mx-auto flex max-w-4xl flex-col gap-6 p-4 sm:p-6" aria-labelledby="scheduled-title">
    <div><PageTitle><span id="scheduled-title">Scheduled messages</span></PageTitle>
      <Hint>Messages posted later by @cron, once or on a repeating rule. Bots can schedule too when they have the tools.</Hint></div>
    <Recurring threads={threads.data ?? []} threadTitle={threadTitle} handle={handle} refresh={() => qc.invalidateQueries({ queryKey: ["recurring"] })} />
    <OneTime threads={threads.data ?? []} threadTitle={threadTitle} refresh={() => qc.invalidateQueries({ queryKey: ["scheduled"] })} />
  </main>;
}

type Threads = { id: string; title: string }[];

function ThreadSelect({ id, threads, value, onChange }: { id: string; threads: Threads; value: string; onChange: (v: string) => void }) {
  return <Select id={id} value={value} onChange={e => onChange(e.target.value)} required>
    <option value="">Choose a thread</option>{threads.map(t => <option key={t.id} value={t.id}>{t.title || t.id}</option>)}
  </Select>;
}

function Recurring({ threads, threadTitle, handle, refresh }: {
  threads: Threads; threadTitle: (id: string) => string; handle: (id: string) => string; refresh: () => void;
}) {
  const list = useQuery({ queryKey: ["recurring"], queryFn: Api.listRecurring });
  const [threadId, setThreadId] = useState("");
  const [content, setContent] = useState("");
  const [preset, setPreset] = useState<Preset>("weekdays");
  const [time, setTime] = useState("09:00");
  const [weekday, setWeekday] = useState(1);
  const [custom, setCustom] = useState("0 9 * * 1-5");
  const [timezone, setTimezone] = useState(browserZone);
  const cron = presetCron(preset, time || "00:00", weekday, custom);
  const preview = useQuery({ queryKey: ["recurring-preview", cron, timezone], queryFn: () => Api.previewRecurring(cron, timezone),
    enabled: !!cron && !!timezone, retry: false });
  const create = useMutation({ mutationFn: () => Api.createRecurring({ thread_id: threadId, content: content.trim(), cron, timezone, to: [] }),
    onSuccess: () => { setContent(""); refresh(); } });
  const update = useMutation({ mutationFn: ({ id, paused }: { id: string; paused: boolean }) => Api.updateRecurring(id, { paused }), onSuccess: refresh, onError: refresh });
  const remove = useMutation({ mutationFn: Api.deleteRecurring, onSuccess: refresh, onError: refresh });
  const submit = (event: FormEvent) => { event.preventDefault(); if (threadId && content.trim() && cron) create.mutate(); };
  const zoneField = <Field label="Timezone"><Input required value={timezone} onChange={e => setTimezone(e.target.value.trim())} /></Field>;
  return <section className="flex flex-col gap-3" aria-label="Recurring messages">
    <SectionTitle>Recurring</SectionTitle>
    <Card><form onSubmit={submit} className="flex flex-col gap-3" aria-label="Create recurring message">
      <Field label="Thread"><ThreadSelect id="recurring-thread" threads={threads} value={threadId} onChange={setThreadId} /></Field>
      <Field label="Message"><Textarea rows={2} required maxLength={20000} value={content} onChange={e => setContent(e.target.value)} /></Field>
      <div className="grid gap-3 sm:grid-cols-3">
        <Field label="Repeat"><Select value={preset} onChange={e => setPreset(e.target.value as Preset)}>
          {presets.map(p => <option key={p.id} value={p.id}>{p.label}</option>)}
        </Select></Field>
        {preset === "custom"
          ? <Field label="Cron expression" hint="minute hour day month weekday"><Input required value={custom} onChange={e => setCustom(e.target.value)} /></Field>
          : <Field label={preset === "hourly" ? "Minute (from the time)" : "Time"}><Input type="time" required value={time} onChange={e => setTime(e.target.value)} /></Field>}
        {preset === "weekly"
          ? <Field label="Weekday"><Select value={weekday} onChange={e => setWeekday(Number(e.target.value))}>{weekdays.map((d, i) => <option key={d} value={i}>{d}</option>)}</Select></Field>
          : zoneField}
      </div>
      {preset === "weekly" && zoneField}
      <p className="text-xs text-muted" aria-live="polite">
        <code>{cron || "…"}</code>{" · "}
        {preview.error ? <span className="text-danger">{String((preview.error as Error).message)}</span>
          : preview.data ? <>next {preview.data.next.map(t => when(t, timezone)).join(", ")}</> : "…"}
      </p>
      <ErrorText error={create.error} />
      <div><Button type="submit" disabled={create.isPending || !!preview.error}>Create schedule</Button></div>
    </form></Card>
    <ErrorText error={list.error || update.error || remove.error} />
    {list.data?.length === 0 && <EmptyState>No recurring messages yet.</EmptyState>}
    {list.data?.map(s => <RecurringCard key={s.id} schedule={s} thread={threadTitle(s.thread_id)} by={handle(s.sender_actor_id)}
      busy={update.isPending || remove.isPending}
      pause={paused => update.mutate({ id: s.id, paused })}
      remove={() => { if (confirm(`Delete the recurring message “${s.content.slice(0, 60)}”? Messages it already posted stay.`)) remove.mutate(s.id); }} />)}
  </section>;
}

function RecurringCard({ schedule: s, thread, by, busy, pause, remove }: {
  schedule: RecurringSchedule; thread: string; by: string; busy: boolean; pause: (paused: boolean) => void; remove: () => void;
}) {
  const paused = s.status === "paused";
  const described = describeCron(s.cron);
  return <article className="rounded-ui border border-line bg-surface p-4" aria-label={`Recurring: ${s.content.slice(0, 40)}`}>
    <div className="flex flex-wrap items-center gap-2">
      <Badge tone={paused ? "zinc" : "green"}>{s.status}</Badge>
      <strong className="text-sm">{described}</strong>
      <span className="text-xs text-muted">{s.timezone}{described !== s.cron && <> · <code>{s.cron}</code></>}</span>
    </div>
    <p className="mt-2 whitespace-pre-wrap break-words font-sans text-sm">{s.content}</p>
    <p className="mt-2 text-xs text-muted">
      Next {paused ? "— (paused)" : when(s.next_due_at, s.timezone)} · last {when(s.last_fired_at, s.timezone)}
      {s.skipped > 0 && <> · {s.skipped} missed</>} · by @{by} in {thread}
      {s.to_handles.length > 0 && <> · to {s.to_handles.map(h => `@${h}`).join(", ")}</>}
    </p>
    <div className="mt-3 flex gap-2">
      <Button size="sm" variant="secondary" disabled={busy} onClick={() => pause(!paused)}>{paused ? "Resume" : "Pause"}</Button>
      <Button size="sm" variant="danger" disabled={busy} onClick={remove}>Delete</Button>
    </div>
  </article>;
}

const localInput = (date: Date) => {
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}T${pad(date.getHours())}:${pad(date.getMinutes())}`;
};

function OneTime({ threads, threadTitle, refresh }: { threads: Threads; threadTitle: (id: string) => string; refresh: () => void }) {
  const q = useQuery({ queryKey: ["scheduled"], queryFn: Api.listScheduled });
  const [threadId, setThreadId] = useState("");
  const [content, setContent] = useState("");
  const [at, setAt] = useState(() => localInput(new Date(Date.now() + 10 * 60_000)));
  const cancel = useMutation({ mutationFn: Api.cancelScheduled, onSuccess: refresh });
  const create = useMutation({ mutationFn: () => Api.createScheduled({ thread_id: threadId, content: content.trim(), due_at: new Date(at).toISOString(), to: [] }),
    onSuccess: () => { setContent(""); refresh(); } });
  const submit = (event: FormEvent) => { event.preventDefault(); if (threadId && content.trim() && at) create.mutate(); };
  return <section className="flex flex-col gap-3" aria-label="One-time messages">
    <SectionTitle>One-time</SectionTitle>
    <Card><form onSubmit={submit} className="flex flex-col gap-3" aria-label="Create scheduled message">
      <Field label="Thread"><ThreadSelect id="schedule-thread" threads={threads} value={threadId} onChange={setThreadId} /></Field>
      <Field label="Message"><Textarea id="schedule-content" rows={2} required maxLength={20000} value={content} onChange={e => setContent(e.target.value)} /></Field>
      <Field label="Send at"><Input id="schedule-at" type="datetime-local" required value={at} onChange={e => setAt(e.target.value)} /></Field>
      <ErrorText error={create.error} />
      <div><Button type="submit" disabled={create.isPending}>Schedule</Button></div>
    </form></Card>
    <ErrorText error={q.error || cancel.error} />
    {q.data?.map(j => <article key={j.id} className="rounded-ui border border-line bg-surface p-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <Badge tone={j.status === "delivered" ? "green" : j.status === "failed" ? "red" : j.status === "pending" ? "blue" : "zinc"}>{j.status}</Badge>
        <time className="text-xs text-muted" dateTime={j.due_at}>{when(j.due_at)}</time>
      </div>
      <p className="mt-2 whitespace-pre-wrap break-words font-sans text-sm">{j.content}</p>
      <p className="mt-1 text-xs text-muted">{threadTitle(j.thread_id)} · attempts {j.attempts}{j.recurring_id && " · from a recurring schedule"}</p>
      {j.last_error && <p className="mt-2 text-xs text-danger">{j.last_error}</p>}
      {j.status === "pending" && <Button className="mt-3" size="sm" variant="secondary" onClick={() => cancel.mutate(j.id)}>Cancel</Button>}
    </article>)}
  </section>;
}
