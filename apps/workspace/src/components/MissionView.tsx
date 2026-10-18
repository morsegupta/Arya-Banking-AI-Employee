import { useEffect, useState } from "react";
import type { Ctx } from "../App";
import { api, inr, type Mission, when } from "../api";
import { NewMission } from "./Home";
import { Empty, MessageCard, MISSION_STYLE, Pill, Section } from "./ui";

export default function MissionView({ ctx }: { ctx: Ctx }) {
  const [m, setM] = useState<Mission | null>(null);
  const [showNew, setShowNew] = useState(false);

  useEffect(() => {
    if (ctx.missionId) api.mission(ctx.missionId).then(setM).catch(() => setM(null));
  }, [ctx.missionId, ctx.refresh]);

  if (!ctx.missionId || showNew) return <div className="max-w-3xl"><NewMission ctx={ctx} /></div>;
  if (!m) return <Empty>Loading…</Empty>;
  const s = m.spec;
  const groups = m.data.groups ?? {};

  return (
    <div className="space-y-6 max-w-5xl">
      <div className="flex items-center gap-3">
        <h1 className="text-xl font-semibold flex-1">Mission & plan</h1>
        <Pill text={m.status} style={MISSION_STYLE[m.status]} />
        <button className="btn-ghost" onClick={() => setShowNew(true)}>New mission</button>
      </div>

      {m.status === "AWAITING_PLAN_APPROVAL" && (
        <div className="rounded-xl border border-amber-200 bg-amber-50 px-5 py-4 flex items-center gap-4">
          <div className="flex-1 text-sm">
            <b>Nothing has been sent.</b> Approving the plan also approves the first messages below, word for word.
          </div>
          <button className="btn-primary" onClick={() => ctx.go("approvals")}>Go to approval</button>
        </div>
      )}

      <Section title="What Arya understood">
        {s ? (
          <dl className="grid grid-cols-[10rem_1fr] gap-y-2 text-sm">
            <dt className="text-muted">Brief</dt><dd>{m.brief}</dd>
            <dt className="text-muted">Objective</dt><dd>{s.objective}</dd>
            <dt className="text-muted">Scope</dt><dd>{s.scope}</dd>
            <dt className="text-muted">Target</dt><dd className="font-medium">{inr(s.target_value)}</dd>
            <dt className="text-muted">Deadline</dt><dd>{s.deadline ? when(s.deadline) : "—"}</dd>
            <dt className="text-muted">Selection</dt><dd className="font-mono text-xs">{JSON.stringify(m.selection)}</dd>
            <dt className="text-muted">Done when</dt><dd><ul className="list-disc pl-4">{s.success_criteria.map((c) => <li key={c}>{c}</li>)}</ul></dd>
            {s.assumptions.length > 0 && <><dt className="text-muted">Assumed</dt>
              <dd><ul className="list-disc pl-4">{s.assumptions.map((c) => <li key={c}>{c}</li>)}</ul></dd></>}
            {s.open_questions.length > 0 && <><dt className="text-amber-700">Questions</dt>
              <dd><ul className="list-disc pl-4">{s.open_questions.map((c) => <li key={c}>{c}</li>)}</ul></dd></>}
          </dl>
        ) : <Empty>Arya is reading the brief…</Empty>}
      </Section>

      {(m.data.excluded?.length ?? 0) > 0 && (
        <Section title="Kept out by policy (code, not the model)">
          <ul className="text-sm space-y-1">
            {m.data.excluded!.map((x) => <li key={x.ref}><b className="font-mono">{x.ref}</b> — {x.reason}</li>)}
          </ul>
        </Section>
      )}

      {(m.data.prepaid?.length ?? 0) > 0 && (
        <Section title="Already paid in the bank (not chased)">
          <ul className="text-sm space-y-1">
            {m.data.prepaid!.map((x) => <li key={x.ref}><b className="font-mono">{x.ref}</b> — {inr(x.amount)} received ({x.record_id});
              Arya records the receipt on its first run instead of sending a reminder</li>)}
          </ul>
        </Section>
      )}

      {m.plan && (
        <Section title="Plan" right={<span className="text-xs text-muted">in scope {inr(m.data.scope_value)} · forecast if all contacted {inr(m.plan.expected_value)}</span>}>
          <p className="text-sm mb-4">{m.plan.summary}</p>
          <table className="w-full text-sm">
            <thead><tr className="text-left text-muted border-b border-line">
              <th className="py-2 w-16">Day</th><th>Batch</th><th>Approach</th><th>Customers</th></tr></thead>
            <tbody>
              {m.plan.batches.map((b, i) => (
                <tr key={i} className="border-b border-line/60 align-top">
                  <td className="py-2">{b.start_day}</td>
                  <td className="py-2 pr-3"><div className="font-medium">{b.name}</div><div className="text-xs text-muted">{b.rationale}</div></td>
                  <td className="py-2"><Pill text={b.approach} /></td>
                  <td className="py-2">{b.groups.map((g) => (
                    <div key={g}>{g} {groups[g]?.name} <span className="text-muted">({groups[g]?.refs.length})</span>
                      {m.data.contacted_groups?.includes(g) && <span className="ml-2 text-xs text-emerald-700">contacted</span>}</div>
                  ))}</td>
                </tr>
              ))}
            </tbody>
          </table>
          {m.plan.risks.length > 0 && <div className="mt-4 text-sm"><div className="label mb-1">Risks</div>
            <ul className="list-disc pl-4">{m.plan.risks.map((r) => <li key={r}>{r}</li>)}</ul></div>}
          {m.plan.problems.length > 0 && <div className="mt-4 text-sm"><div className="label mb-1">Corrected by code</div>
            <ul className="list-disc pl-4 text-muted">{m.plan.problems.map((r) => <li key={r}>{r}</li>)}</ul></div>}
        </Section>
      )}

      {m.data.first_batch && Object.keys(m.data.first_batch).length > 0 && (
        <Section title="First-batch messages" right={<span className="text-xs text-muted">sent exactly as approved</span>}>
          <div className="space-y-4">
            {Object.entries(m.data.first_batch).map(([g, d]) => (
              <MessageCard key={g} to={`${groups[g]?.name ?? g} · ${d.to}`} subject={d.subject} body={d.body} attachments={d.attachments}
                meta={<span><Pill text={d.approach} /> {d.channel}</span>} />
            ))}
          </div>
        </Section>
      )}

      {(m.data.actions?.length ?? 0) > 0 && (
        <Section title="What Arya decided on its reviews">
          <ul className="text-sm space-y-1">
            {m.data.actions!.slice().reverse().map((a, i) => (
              <li key={i}><span className="text-muted">{a.date}</span> <b>{a.lever.replaceAll("_", " ")}</b> {a.groups.join(", ")} — {a.result}</li>
            ))}
          </ul>
        </Section>
      )}

      <div className="text-xs text-muted">LLM tokens used {m.budget.used.toLocaleString()} of {m.budget.max.toLocaleString()} for this mission</div>
    </div>
  );
}
