import { useState, type FormEvent } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, useSearchParams } from "react-router-dom";
import { Api, getActorHandle } from "../api/client";
import type { Actor, BoardCard, CardStatus, Thread } from "../api/types";
import { useBusEvents } from "../api/sse";
import MarkdownContent from "../components/MarkdownContent";
import { Button, Dialog, EmptyState, ErrorText, Field, Hint, Input, PageTitle, Select, Spinner, Textarea } from "../components/ui";

const columns: { id: CardStatus; label: string }[] = [
  { id: "backlog", label: "Backlog" }, { id: "ready", label: "Ready" },
  { id: "doing", label: "Doing" }, { id: "review", label: "Review" },
  { id: "done", label: "Done" }, { id: "blocked", label: "Blocked" },
];

export default function BoardsPage() {
  const qc = useQueryClient();
  const [params, setParams] = useSearchParams();
  const boards = useQuery({ queryKey: ["boards"], queryFn: Api.listBoards });
  const boardId = params.get("board") ?? boards.data?.[0]?.id ?? "";
  const cards = useQuery({ queryKey: ["board-cards", boardId], queryFn: () => Api.listCards(boardId), enabled: !!boardId });
  const actors = useQuery({ queryKey: ["actors"], queryFn: Api.listActors });
  const threads = useQuery({ queryKey: ["threads"], queryFn: Api.listThreads });
  const [newBoard, setNewBoard] = useState(false);
  const [boardName, setBoardName] = useState("");
  const [newCard, setNewCard] = useState(false);
  const [cardTitle, setCardTitle] = useState("");
  const [selected, setSelected] = useState<BoardCard | null>(null);
  const refresh = () => {
    qc.invalidateQueries({ queryKey: ["boards"] });
    qc.invalidateQueries({ queryKey: ["board-cards"] });
    qc.invalidateQueries({ queryKey: ["card-history"] });
  };
  useBusEvents(null, e => { if (e.event === "board.updated") refresh(); }, refresh);
  const createBoard = useMutation({ mutationFn: Api.createBoard, onSuccess: board => {
    setParams({ board: board.id }); setNewBoard(false); setBoardName(""); refresh();
  } });
  const createCard = useMutation({ mutationFn: () => Api.createCard(boardId, { title: cardTitle.trim() }), onSuccess: card => {
    setNewCard(false); setCardTitle(""); setSelected(card); refresh();
  } });
  const claim = useMutation({ mutationFn: Api.claimCard, onSuccess: refresh, onError: refresh });
  const me = actors.data?.find(a => a.handle === getActorHandle());
  const board = boards.data?.find(b => b.id === boardId);
  return <main className="flex h-full flex-col gap-4 overflow-auto p-4 sm:p-6">
    <PageTitle actions={<Button variant="secondary" onClick={() => setNewBoard(true)}>New board</Button>}>Boards</PageTitle>
    <Hint>Shared tasks for people and bots. Open a card to assign work, link a discussion, or record a result.</Hint>
    <ErrorText error={boards.error || cards.error || actors.error || threads.error || claim.error} />
    {boards.isPending && <Spinner />}
    {boards.data?.length === 0 && <EmptyState>Create a board for your first project.</EmptyState>}
    {!!boards.data?.length && <div className="flex items-end gap-3">
      <div className="min-w-0 flex-1 max-w-sm"><Field label="Project board"><Select value={boardId} onChange={e => { setParams({ board: e.target.value }); setSelected(null); }}>
        {boards.data.map(b => <option key={b.id} value={b.id}>{b.name}</option>)}
      </Select></Field></div>
      <Button onClick={() => setNewCard(true)} disabled={!board}>New card</Button>
    </div>}
    {board?.description && <Hint>{board.description}</Hint>}
    {cards.isFetching && <span className="text-xs text-muted">Refreshing…</span>}
    {board && <div className="grid min-h-72 grid-cols-1 items-start gap-3 sm:grid-cols-2 xl:grid-cols-3 2xl:grid-cols-6" aria-label={`${board.name} board`}>
      {columns.map(column => {
        const items = cards.data?.filter(c => c.status === column.id) ?? [];
        return <section key={column.id} className="min-w-0 rounded-ui border border-line bg-sunken/30 p-3" aria-label={column.label}>
          <h2 className="mb-3 flex justify-between text-xs font-semibold">{column.label}<span className="text-muted">{items.length}</span></h2>
          <div className="space-y-2">{items.map(card => <article key={card.id} className="rounded-ui border border-line bg-surface p-3">
            <button className="w-full break-words text-left text-sm font-medium text-fg hover:text-accent" onClick={() => setSelected(card)}>{card.title}</button>
            <p className="mt-2 text-xs text-muted">{actors.data?.find(a => a.id === card.assignee_id)?.name ?? "Unassigned"}</p>
            {card.blocked_reason && <p className="mt-2 break-words text-xs text-warn">{card.blocked_reason}</p>}
            <div className="mt-2 flex flex-wrap items-center gap-2">
              {card.thread_id && <Link className="text-xs text-accent underline" to={`/threads/${card.thread_id}`}>Discussion</Link>}
              {card.status === "ready" && (!card.assignee_id || card.assignee_id === me?.id) && <Button size="sm" variant="secondary" disabled={claim.isPending} onClick={() => claim.mutate(card.id)}>Claim</Button>}
            </div>
          </article>)}{items.length === 0 && <p className="py-3 text-xs text-faint">No cards</p>}</div>
        </section>;
      })}
    </div>}
    {newBoard && <Dialog title="New board" titleId="new-board" onClose={() => setNewBoard(false)} as="form" onSubmit={e => { e.preventDefault(); createBoard.mutate({ name: boardName.trim() }); }}>
      <Field label="Name"><Input autoFocus required maxLength={120} value={boardName} onChange={e => setBoardName(e.target.value)} /></Field>
      <ErrorText error={createBoard.error} /><Button type="submit" disabled={createBoard.isPending || !boardName.trim()}>Create board</Button>
    </Dialog>}
    {newCard && <Dialog title="New card" titleId="new-card" onClose={() => setNewCard(false)} as="form" onSubmit={e => { e.preventDefault(); createCard.mutate(); }}>
      <Field label="Title"><Input autoFocus required maxLength={200} value={cardTitle} onChange={e => setCardTitle(e.target.value)} /></Field>
      <ErrorText error={createCard.error} /><Button type="submit" disabled={createCard.isPending || !cardTitle.trim()}>Create card</Button>
    </Dialog>}
    {selected && <CardEditor key={`${selected.id}:${selected.version}`} card={selected} actors={actors.data ?? []} threads={threads.data ?? []}
      close={() => setSelected(null)} saved={() => { setSelected(null); refresh(); }} reload={setSelected} />}
  </main>;
}

function CardEditor({ card, actors, threads, close, saved, reload }: {
  card: BoardCard; actors: Actor[]; threads: Thread[]; close: () => void; saved: () => void; reload: (card: BoardCard) => void;
}) {
  const [title, setTitle] = useState(card.title);
  const [description, setDescription] = useState(card.description);
  const [status, setStatus] = useState(card.status);
  const [assignee, setAssignee] = useState(card.assignee_id ?? "");
  const [thread, setThread] = useState(card.thread_id ?? "");
  const [result, setResult] = useState(card.result);
  const [reason, setReason] = useState(card.blocked_reason);
  const history = useQuery({ queryKey: ["card-history", card.id], queryFn: () => Api.cardHistory(card.id) });
  const reset = useMutation({ mutationFn: () => Api.getCard(card.id), onSuccess: reload });
  const save = useMutation({ mutationFn: () => Api.updateCard(card.id, {
    expected_version: card.version, title, description, status,
    ...(assignee !== (card.assignee_id ?? "") ? { assignee_handle: actors.find(a => a.id === assignee)?.handle ?? null } : {}),
    ...(thread !== (card.thread_id ?? "") ? { thread_id: thread || null } : {}),
    result, blocked_reason: status === "blocked" ? reason : "",
  }), onSuccess: saved });
  const submit = (e: FormEvent) => { e.preventDefault(); save.mutate(); };
  return <Dialog title="Task card" titleId="edit-card" onClose={close} as="form" onSubmit={submit} className="max-h-[90dvh] overflow-y-auto sm:max-w-2xl">
    <Field label="Title"><Input required maxLength={200} value={title} onChange={e => setTitle(e.target.value)} /></Field>
    <Field label="Description"><Textarea rows={3} maxLength={20000} value={description} onChange={e => setDescription(e.target.value)} /></Field>
    <div className="grid grid-cols-2 gap-3">
      <Field label="Status"><Select value={status} onChange={e => setStatus(e.target.value as CardStatus)}>{columns.map(c => <option key={c.id} value={c.id}>{c.label}</option>)}</Select></Field>
      <Field label="Assignee"><Select value={assignee} onChange={e => setAssignee(e.target.value)}><option value="">Unassigned</option>{actors.filter(a => a.enabled || a.id === assignee).map(a => <option key={a.id} value={a.id}>{a.name}{a.enabled ? "" : " (disabled)"}</option>)}</Select></Field>
    </div>
    {status === "blocked" && <Field label="Blocked reason"><Textarea required rows={2} maxLength={2000} value={reason} onChange={e => setReason(e.target.value)} /></Field>}
    <Field label="Discussion"><Select value={thread} onChange={e => setThread(e.target.value)}><option value="">None</option>
      {thread && !threads.some(t => t.id === thread) && <option value={thread}>Existing discussion</option>}
      {threads.map(t => <option key={t.id} value={t.id}>{t.title || t.id}</option>)}
    </Select></Field>
    <Field label="Result" hint="Summarize the outcome and link supporting work. Required for Review and Done."><Textarea rows={4} maxLength={40000} value={result} onChange={e => setResult(e.target.value)} required={status === "review" || status === "done"} /></Field>
    {!!result && <details><summary className="cursor-pointer text-xs text-muted">Preview result</summary><MarkdownContent content={result} /></details>}
    <ErrorText error={save.error || reset.error} />
    {save.isError && <Button variant="secondary" onClick={() => reset.mutate()} disabled={reset.isPending}>Reload card (discard edits)</Button>}
    <div className="flex gap-2"><Button type="submit" disabled={save.isPending}>Save card</Button><Button variant="secondary" onClick={close}>Cancel</Button></div>
    <details><summary className="cursor-pointer text-xs text-muted">Change history</summary>
      <ErrorText error={history.error} />
      <ol className="mt-3 space-y-3">{history.data?.map(event => <li key={event.id} className="border-l border-line pl-3 text-xs">
        <strong>@{event.actor_handle}</strong> {event.action} · <time dateTime={event.created_at}>{new Date(event.created_at).toLocaleString()}</time>
        <dl className="mt-1 space-y-1 text-muted">{Object.entries(event.changes).map(([field, value]) => <div key={field}><dt className="inline font-medium">{field.replaceAll("_", " ")}: </dt><dd className="inline whitespace-pre-wrap break-words">{String(value ?? "cleared")}</dd></div>)}</dl>
      </li>)}</ol>
    </details>
  </Dialog>;
}
