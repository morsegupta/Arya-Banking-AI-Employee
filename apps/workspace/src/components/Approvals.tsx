import { useEffect, useState } from "react";
import type { Ctx } from "../App";
import { api, type Approval, when } from "../api";
import { Empty, MessageCard, Pill, Section } from "./ui";

const KIND_LABEL: Record<string, string> = {
  mission_plan: "Plan and first messages", concession: "Concession", payment_plan: "Payment plan",
  review_quarantine: "Quarantined message", failure: "Arya is stuck", budget: "Budget",
};

const NOTE_HINT: Record<string, string> = {
  mission_plan: "Note (on reject, Arya redoes the plan and the drafts using it)",
  concession: "Note for the reply, e.g. the exact discount you allow (optional)",
  payment_plan: "Plan terms to offer, e.g. two equal instalments by 15 Nov",
  review_quarantine: "Why (recorded with your decision)",
};

function Details({ a }: { a: Approval }) {
  const p = a.payload;
  if (a.kind === "tool_call") {
    return (
      <div className="space-y-2">
        <div className="text-sm text-muted">Held by policy: {p.reason}</div>
        {p.args?.body ? <MessageCard to={p.args.to} subject={p.args.subject} body={p.args.body} attachments={p.args.attachments} />
          : <pre className="text-xs bg-slate-50 rounded p-3 overflow-auto">{JSON.stringify(p.args, null, 2)}</pre>}
      </div>
    );
  }
  switch (p.action) {
    case "mission_plan":
      return (
        <div className="space-y-3">
          <p className="text-sm">{p.summary}</p>
          <div className="text-sm">
            {(p.batches ?? []).map((b: any, i: number) => (
              <div key={i}>Day {b.start_day}: <b>{b.name}</b> — {b.groups.join(", ")} <span className="text-muted">({b.approach})</span></div>
            ))}
          </div>
          {Object.keys(p.messages ?? {}).length > 0 && <div className="label pt-2">Messages to send on approval, word for word</div>}
          {Object.entries(p.messages ?? {}).map(([g, d]: [string, any]) => (
            <MessageCard key={g} to={`${g} · ${d.to}`} subject={d.subject} body={d.body} attachments={d.attachments}
              meta={<span>{d.channel} · {d.approach}</span>} />
          ))}
        </div>
      );
    case "review_quarantine":
      return (
        <div className="text-sm space-y-2">
          <div>From <b>{p.message?.from_addr}</b> on {p.message?.channel}. Score {p.screening?.score} {p.screening?.pattern_hits?.length ? `· ${p.screening.pattern_hits.length} suspicious pattern(s)` : ""}</div>
          <details className="rounded border border-red-200 bg-red-50 p-3">
            <summary className="cursor-pointer text-red-800">Show the withheld message (agents never saw it)</summary>
            <pre className="mt-2 whitespace-pre-wrap text-xs">{p.message?.body}</pre>
          </details>
        </div>
      );
    case "concession":
      return null;                         // the question already quotes the customer
    case "failure":
      return <pre className="text-xs bg-slate-50 rounded p-3 overflow-auto whitespace-pre-wrap">{JSON.stringify(p.detail, null, 2)}</pre>;
    case "budget":
      return <div className="text-sm">Used {Number(p.used).toLocaleString()} of {Number(p.max).toLocaleString()} tokens.</div>;
    case "payment_plan":
      return <div className="text-sm">{p.rationale}</div>;
    default:
      return null;
  }
}

function Card({ a, ctx }: { a: Approval; ctx: Ctx }) {
  const [note, setNote] = useState("");
  const busy = ctx.job?.status === "running";
  const pending = a.status === "PENDING";
  const decide = (approve: boolean) => ctx.startJob(api.decide(a.id, approve, ctx.reviewer, note));
  return (
    <div className="card">
      <div className="px-5 py-3 border-b border-line flex items-center gap-3">
        <Pill text={KIND_LABEL[a.payload.action] ?? (a.kind === "tool_call" ? "Held action" : a.kind)} style="bg-amber-100 text-amber-800" />
        <div className="font-medium text-sm flex-1">{a.title}</div>
        {a.ref && <button className="text-xs font-mono text-brand" onClick={() => a.entity_id && ctx.openEntity(a.entity_id)}>{a.ref}</button>}
        <span className="text-xs text-muted">{when(a.requested_at)}</span>
      </div>
      <div className="p-5 space-y-4">
        {a.payload.question && <div className="text-sm font-medium">{a.payload.question}</div>}
        <Details a={a} />
        {pending ? (
          <div className="flex items-center gap-3 pt-2 border-t border-line">
            <input className="input" placeholder={NOTE_HINT[a.payload.action] ?? "Note (optional, recorded with your decision)"}
              value={note} onChange={(e) => setNote(e.target.value)} />
            <button className="btn-danger" disabled={busy || !ctx.reviewer} onClick={() => decide(false)}>Reject</button>
            <button className="btn-primary" disabled={busy || !ctx.reviewer} onClick={() => decide(true)}>Approve</button>
          </div>
        ) : (
          <div className="text-xs text-muted">{a.status.toLowerCase()} by {a.decided_by}{a.note ? ` — ${a.note}` : ""}</div>
        )}
        {pending && !ctx.reviewer && <div className="text-xs text-amber-700">Enter your name at the top right: every decision records who made it.</div>}
      </div>
    </div>
  );
}

export default function Approvals({ ctx }: { ctx: Ctx }) {
  const [items, setItems] = useState<Approval[] | null>(null);
  const [showDone, setShowDone] = useState(false);
  useEffect(() => {
    api.approvals(showDone ? "" : "PENDING").then(setItems).catch(() => setItems([]));
  }, [ctx.refresh, showDone]);

  return (
    <div className="space-y-4 max-w-4xl">
      <div className="flex items-center gap-3">
        <h1 className="text-xl font-semibold flex-1">Approvals</h1>
        <label className="text-sm text-muted flex items-center gap-2">
          <input type="checkbox" checked={showDone} onChange={(e) => setShowDone(e.target.checked)} /> show decided
        </label>
      </div>
      <p className="text-sm text-muted">Only you can decide these. Arya holds the exact action; approving runs it exactly as shown.</p>
      {items === null ? <Empty>Loading…</Empty> : items.length === 0 ? (
        <Section title="Inbox"><Empty>Nothing waiting for you.</Empty></Section>
      ) : items.map((a) => <Card key={a.id} a={a} ctx={ctx} />)}
    </div>
  );
}
