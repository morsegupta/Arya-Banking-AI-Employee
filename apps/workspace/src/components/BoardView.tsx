import { useEffect, useState } from "react";
import type { Ctx } from "../App";
import { api, type Board, inr } from "../api";
import { Empty, Pill, STATE_STYLE } from "./ui";

// Main flow left to right; side states appear only when something is in them.
const MAIN = ["OVERDUE", "CONTACTED", "ACKNOWLEDGED", "PROMISED", "PARTIALLY_PAID", "PAID"];

export default function BoardView({ ctx }: { ctx: Ctx }) {
  const [board, setBoard] = useState<Board | null>(null);
  const [q, setQ] = useState("");

  useEffect(() => {
    if (ctx.missionId) api.board(ctx.missionId).then(setBoard).catch(() => setBoard(null));
  }, [ctx.missionId, ctx.refresh]);

  if (!ctx.missionId) return <Empty>Start a mission first.</Empty>;
  if (!board) return <Empty>Loading…</Empty>;
  const match = (s: string) => s.toLowerCase().includes(q.toLowerCase());
  const ents = board.entities.filter((e) => !q || match(e.ref) || match(e.customer ?? ""));
  const side = board.states.filter((s) => !MAIN.includes(s) && ents.some((e) => e.state === s));
  const columns = [...MAIN, ...side];

  return (
    <div className="space-y-4">
      <div className="flex items-center gap-4">
        <h1 className="text-xl font-semibold flex-1">Invoices by stage</h1>
        <input className="input max-w-xs" placeholder="Search invoice or customer" value={q} onChange={(e) => setQ(e.target.value)} />
      </div>
      <div className="flex gap-4 overflow-x-auto pb-4">
        {columns.map((state) => {
          const items = ents.filter((e) => e.state === state);
          const total = items.reduce((t, e) => t + (state === "PAID" ? e.value : e.balance), 0);
          return (
            <div key={state} className="w-64 shrink-0">
              <div className="flex items-center gap-2 mb-2 px-1">
                <Pill text={state} style={STATE_STYLE[state]} />
                <span className="text-xs text-muted">{items.length}</span>
                <span className="flex-1" />
                <span className="text-xs tabular-nums text-muted">{inr(total)}</span>
              </div>
              <div className="space-y-2 min-h-24 rounded-xl bg-slate-100/60 p-2">
                {items.map((e) => (
                  <button key={e.id} onClick={() => ctx.openEntity(e.id)}
                    className="w-full text-left card p-3 hover:border-brand hover:shadow-sm transition">
                    <div className="flex items-center gap-2">
                      <span className="font-mono text-xs font-semibold">{e.ref}</span>
                      <span className="flex-1" />
                      {e.quarantined && <span className="pill bg-red-100 text-red-700" title="quarantined">⚠</span>}
                    </div>
                    <div className="text-sm mt-1 truncate">{e.customer}</div>
                    <div className="mt-2 flex items-baseline gap-2">
                      <span className="font-semibold tabular-nums">{inr(state === "PAID" ? e.value : e.balance)}</span>
                      <span className="text-xs text-muted">{e.days_overdue}d overdue</span>
                    </div>
                    {e.promise_date && state !== "PAID" && (
                      <div className="mt-1 text-xs text-amber-700">promised {e.promise_date}{e.promise_amount ? ` · ${inr(e.promise_amount)}` : ""}</div>
                    )}
                    {e.escalation_reason && <div className="mt-1 text-xs text-red-700 truncate">{e.escalation_reason}</div>}
                    {e.last_contact && <div className="mt-1 text-xs text-muted">last contact: {e.last_contact}</div>}
                  </button>
                ))}
                {!items.length && <div className="text-xs text-muted text-center py-6">—</div>}
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}
