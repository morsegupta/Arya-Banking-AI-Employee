import { useEffect, useState } from "react";
import type { Ctx } from "../App";
import { api, type Board, inr, inrShort, type Mission, when } from "../api";
import { Empty, MISSION_STYLE, Pill, Section, STATE_STYLE } from "./ui";

const EXAMPLE = "Collect the 45+ day overdue invoices — target ₹12L by Friday.";

export function NewMission({ ctx }: { ctx: Ctx }) {
  const [brief, setBrief] = useState(EXAMPLE);
  const busy = ctx.job?.status === "running";
  return (
    <Section title="Give Arya a brief">
      <p className="text-sm text-muted mb-3">One sentence is enough. Arya reads the credit policy, picks the invoices, ranks customers,
        drafts the first messages and asks you to approve the plan before anything is sent.</p>
      <textarea className="input h-24" value={brief} onChange={(e) => setBrief(e.target.value)} />
      <div className="mt-3 flex justify-end">
        <button className="btn-primary" disabled={busy || brief.trim().length < 5}
          onClick={() => ctx.startJob(api.createMission(brief.trim())).then(() => ctx.go("mission"))}>
          {busy ? "Arya is working…" : "Start mission"}
        </button>
      </div>
    </Section>
  );
}

function Kpi({ label, value, sub, accent }: { label: string; value: string; sub?: string; accent?: string }) {
  return (
    <div className="card p-5">
      <div className="label">{label}</div>
      <div className={`mt-2 text-2xl font-semibold tabular-nums ${accent ?? ""}`}>{value}</div>
      {sub && <div className="mt-1 text-xs text-muted">{sub}</div>}
    </div>
  );
}

export default function Home({ ctx }: { ctx: Ctx }) {
  const [m, setM] = useState<Mission | null>(null);
  const [board, setBoard] = useState<Board | null>(null);

  useEffect(() => {
    if (!ctx.missionId) return;
    api.mission(ctx.missionId).then(setM).catch(() => setM(null));
    api.board(ctx.missionId).then(setBoard).catch(() => setBoard(null));
  }, [ctx.missionId, ctx.refresh]);

  if (!ctx.missionId) return <div className="max-w-3xl"><NewMission ctx={ctx} /></div>;
  if (!m) return <Empty>Loading…</Empty>;

  const p = m.progress;
  const target = p?.target ?? m.spec?.target_value ?? null;
  const pct = target ? Math.min(100, Math.round(((p?.verified ?? 0) / target) * 100)) : 0;
  const expPct = target ? Math.min(100, Math.round(((p?.expected ?? 0) / target) * 100)) : 0;
  const busy = ctx.job?.status === "running";
  const counts: Record<string, { n: number; v: number }> = {};
  for (const e of board?.entities ?? []) {
    counts[e.state] = counts[e.state] ?? { n: 0, v: 0 };
    counts[e.state].n += 1;
    counts[e.state].v += e.state === "PAID" ? e.value : e.balance;
  }
  const missionFeed = ctx.feed.filter((f) => f.mission_id === m.id).slice(-12).reverse();

  return (
    <div className="space-y-6 max-w-6xl">
      <div className="flex items-start gap-4">
        <div className="flex-1">
          <div className="flex items-center gap-3">
            <h1 className="text-xl font-semibold">{m.spec?.objective ?? m.brief}</h1>
            <Pill text={m.status} style={MISSION_STYLE[m.status]} />
          </div>
          <div className="text-sm text-muted mt-1">
            {m.spec?.deadline ? `Deadline ${when(m.spec.deadline)}` : "No deadline"} · {m.brief}
          </div>
        </div>
        {m.status === "ACTIVE" && ctx.status?.sandbox_sim && (
          <button className="btn-primary" disabled={busy} onClick={() => ctx.startJob(api.day(m.id))}
            title="Sandbox only: customers act, Arya works, the clock moves one day">
            ▶ Simulate a day
          </button>
        )}
        {m.status === "ACTIVE" && (
          <button className="btn-ghost" disabled={busy} onClick={() => ctx.startJob(api.tick(m.id))}>Run Arya now</button>
        )}
        {m.status === "AWAITING_PLAN_APPROVAL" && (
          <button className="btn-primary" onClick={() => ctx.go("approvals")}>Review plan & first messages</button>
        )}
        {m.status === "PAUSED" && (
          <button className="btn-primary" disabled={busy || !ctx.reviewer}
            onClick={() => ctx.startJob(api.resume(m.id, ctx.reviewer, m.budget.max))}>Resume (double budget)</button>
        )}
      </div>

      <div className="card p-6">
        <div className="flex items-end gap-6">
          <div>
            <div className="label">Collected and verified in the bank</div>
            <div className="mt-1 text-4xl font-semibold tabular-nums text-emerald-700">{inrShort(p?.verified ?? 0)}</div>
          </div>
          <div className="text-muted pb-1">of {inrShort(target)} target</div>
          <div className="flex-1" />
          <div className="text-right text-sm">
            <div className="text-muted">Forecast by deadline</div>
            <div className="font-semibold tabular-nums">{inrShort(p?.expected)}</div>
          </div>
        </div>
        <div className="mt-4 h-3 rounded-full bg-slate-100 relative overflow-hidden">
          <div className="absolute inset-y-0 left-0 bg-emerald-200" style={{ width: `${expPct}%` }} />
          <div className="absolute inset-y-0 left-0 bg-emerald-600 transition-all" style={{ width: `${pct}%` }} />
        </div>
        <div className="mt-2 flex text-xs text-muted">
          <span>{pct}% verified</span><span className="flex-1" />
          <span>only money matched in the bank, posted in the ERP and verified counts — never promises</span>
        </div>
      </div>

      <div className="grid grid-cols-4 gap-4">
        <Kpi label="Gap to target" value={inrShort(p?.gap)} sub={p?.expected_shortfall && p.expected_shortfall > 0
          ? `forecast short by ${inrShort(p.expected_shortfall)}` : "forecast meets target"} />
        <Kpi label="Promised" value={inrShort(counts.PROMISED?.v ?? 0)} sub={`${counts.PROMISED?.n ?? 0} invoices with a firm date`} accent="text-amber-700" />
        <Kpi label="In scope" value={inrShort(m.data.scope_value)} sub={`${board?.entities.length ?? 0} invoices · ${Object.keys(m.data.groups ?? {}).length} customers`} />
        <Kpi label="Waiting for you" value={String(ctx.status?.pending_approvals ?? 0)} sub="approvals in the inbox"
          accent={(ctx.status?.pending_approvals ?? 0) > 0 ? "text-amber-700" : ""} />
      </div>

      <div className="grid grid-cols-5 gap-6">
        <div className="col-span-3">
          <Section title="Pipeline" right={<button className="text-xs text-brand" onClick={() => ctx.go("board")}>open board →</button>}>
            {board ? (
              <div className="space-y-2">
                {board.states.filter((s) => counts[s]).map((s) => {
                  const max = Math.max(...Object.values(counts).map((c) => c.v), 1);
                  return (
                    <div key={s} className="flex items-center gap-3 text-sm">
                      <div className="w-36"><Pill text={s} style={STATE_STYLE[s]} /></div>
                      <div className="flex-1 h-2 rounded bg-slate-100">
                        <div className="h-2 rounded bg-slate-400" style={{ width: `${(counts[s].v / max) * 100}%` }} />
                      </div>
                      <div className="w-10 text-right tabular-nums text-muted">{counts[s].n}</div>
                      <div className="w-28 text-right tabular-nums">{inr(counts[s].v)}</div>
                    </div>
                  );
                })}
              </div>
            ) : <Empty>No invoices yet</Empty>}
          </Section>
        </div>
        <div className="col-span-2">
          <Section title="Live activity" right={<button className="text-xs text-brand" onClick={() => ctx.go("activity")}>all →</button>}>
            {missionFeed.length ? (
              <ul className="space-y-2 text-sm">
                {missionFeed.map((f) => (
                  <li key={f.seq} className="flex gap-2">
                    <span className="text-xs text-muted w-24 shrink-0 tabular-nums">{when(f.at)}</span>
                    <button className="text-left hover:text-brand" disabled={!f.entity_id}
                      onClick={() => f.entity_id && ctx.openEntity(f.entity_id)}>{f.text}</button>
                  </li>
                ))}
              </ul>
            ) : <Empty>Nothing yet</Empty>}
          </Section>
        </div>
      </div>
    </div>
  );
}
