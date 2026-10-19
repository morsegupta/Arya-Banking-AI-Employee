import { useEffect, useState } from "react";
import type { Ctx } from "../App";
import { api, type MemoryItem, when } from "../api";
import { Empty, Pill, Section } from "./ui";

const KIND_STYLE: Record<string, string> = {
  fact: "bg-slate-100 text-slate-700",
  lesson: "bg-violet-100 text-violet-800",
  playbook: "bg-sky-100 text-sky-800",
};

// What Arya remembers across missions. Facts are computed by code from what happened; lessons are
// written by the Memory Writer and cite the episodes behind them; playbooks record how a task was done
// when the scripted route failed. A human can pin (vouch for) or reject (never use again) any of them.
export default function MemoryView({ ctx }: { ctx: Ctx }) {
  const [items, setItems] = useState<MemoryItem[] | null>(null);
  const [all, setAll] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const busy = ctx.job?.status === "running";

  const load = () => api.memory(all).then(setItems).catch((e) => setErr(String(e)));
  useEffect(() => { load(); }, [ctx.refresh, all]);   // eslint-disable-line react-hooks/exhaustive-deps

  const decide = async (id: string, status: "pinned" | "rejected" | "active") => {
    setErr(null);
    try {
      await api.decideMemory(id, status, ctx.reviewer);
      await load();
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    }
  };

  const scopes = new Map<string, MemoryItem[]>();
  for (const m of items ?? []) scopes.set(m.scope, [...(scopes.get(m.scope) ?? []), m]);

  return (
    <div className="space-y-4 max-w-5xl">
      <div className="flex items-center gap-4">
        <h1 className="text-xl font-semibold flex-1">What Arya remembers</h1>
        <label className="text-xs text-muted flex items-center gap-2">
          <input type="checkbox" checked={all} onChange={(e) => setAll(e.target.checked)} /> show rejected
        </label>
        {ctx.missionId && (
          <button className="btn-ghost" disabled={busy} onClick={() => ctx.startJob(api.learn(ctx.missionId!))}
            title="Memory Writer: facts from this mission's events, then lessons">Learn from this mission</button>
        )}
      </div>
      <p className="text-sm text-muted">Memory survives a reset of the sandbox. It shapes planning (customer history in the plan
        and the forecast), which channel is tried first, and the notes the Operator reads; it never decides a transition.
        {!ctx.reviewer && " Type your name top right to pin or reject."}</p>
      {err && <div className="text-sm text-rose-700">{err}</div>}
      {items === null ? <Empty>Loading…</Empty> : items.length === 0 ? <Empty>Nothing remembered yet.</Empty> : (
        [...scopes.entries()].map(([scope, ms]) => (
          <Section key={scope} title={scope}>
            <ul className="divide-y divide-line">
              {ms.map((m) => (
                <li key={m.id} className="py-2 flex items-start gap-3 text-sm">
                  <Pill text={m.kind} style={KIND_STYLE[m.kind]} />
                  <div className={`flex-1 ${m.status === "rejected" ? "line-through text-muted" : ""}`}>
                    {m.text}
                    <div className="text-xs text-muted mt-0.5">
                      {m.status !== "active" && <b>{m.status}{m.decided_by ? ` by ${m.decided_by}` : ""} · </b>}
                      updated {when(m.updated_at)}{m.source ? ` · from ${m.source}` : ""}
                    </div>
                  </div>
                  <div className="flex gap-2 shrink-0">
                    {m.status !== "pinned" && <button className="text-xs text-brand disabled:opacity-40" disabled={!ctx.reviewer}
                      onClick={() => decide(m.id, "pinned")}>pin</button>}
                    {m.status !== "rejected" && <button className="text-xs text-rose-700 disabled:opacity-40" disabled={!ctx.reviewer}
                      onClick={() => decide(m.id, "rejected")}>reject</button>}
                    {m.status !== "active" && <button className="text-xs text-muted disabled:opacity-40" disabled={!ctx.reviewer}
                      onClick={() => decide(m.id, "active")}>restore</button>}
                  </div>
                </li>
              ))}
            </ul>
          </Section>
        ))
      )}
    </div>
  );
}
