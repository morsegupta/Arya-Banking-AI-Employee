import { useState } from "react";
import type { Ctx } from "../App";
import { when } from "../api";
import { Empty, Section } from "./ui";

const FILTERS: Record<string, (t: string) => boolean> = {
  all: () => true,
  pipeline: (t) => t.startsWith("entity.") || t.startsWith("settlement.") || t.startsWith("verification."),
  messages: (t) => t.startsWith("message.") || t === "mission.group_contacted",
  safety: (t) => t.startsWith("security.") || t.startsWith("policy.") || t.startsWith("approval."),
  reliability: (t) => t.startsWith("fallback.") || t.startsWith("statement.") || t.startsWith("task.") || t.startsWith("playbook."),
};

export default function Activity({ ctx }: { ctx: Ctx }) {
  const [filter, setFilter] = useState("all");
  const items = ctx.feed.filter((f) => FILTERS[filter](f.type)).slice().reverse();
  return (
    <div className="space-y-4 max-w-5xl">
      <div className="flex items-center gap-3">
        <h1 className="text-xl font-semibold flex-1">Activity</h1>
        {Object.keys(FILTERS).map((f) => (
          <button key={f} onClick={() => setFilter(f)} className={`pill ${filter === f ? "bg-brand text-white" : "bg-white border border-line text-slate-600"}`}>{f}</button>
        ))}
      </div>
      <Section title="Audit log (hash-chained), newest first">
        {items.length ? (
          <table className="w-full text-sm">
            <tbody>
              {items.map((f) => (
                <tr key={f.seq} className="border-b border-line/60 align-top">
                  <td className="py-1.5 pr-3 text-xs text-muted tabular-nums w-10">#{f.seq}</td>
                  <td className="py-1.5 pr-3 text-xs text-muted whitespace-nowrap">{when(f.at)}</td>
                  <td className="py-1.5 pr-3 text-xs font-mono text-slate-500 whitespace-nowrap">{f.type}</td>
                  <td className="py-1.5">
                    {f.entity_id ? <button className="text-left hover:text-brand" onClick={() => ctx.openEntity(f.entity_id!)}>{f.text}</button> : f.text}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : <Empty>No events yet.</Empty>}
      </Section>
    </div>
  );
}
