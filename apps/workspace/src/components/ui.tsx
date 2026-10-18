import type { ReactNode } from "react";

export const STATE_STYLE: Record<string, string> = {
  OVERDUE: "bg-slate-100 text-slate-700",
  CONTACTED: "bg-sky-100 text-sky-800",
  ACKNOWLEDGED: "bg-indigo-100 text-indigo-800",
  PROMISED: "bg-amber-100 text-amber-800",
  PARTIALLY_PAID: "bg-lime-100 text-lime-800",
  PAID: "bg-emerald-100 text-emerald-800",
  BROKEN_PROMISE: "bg-orange-100 text-orange-800",
  DISPUTED: "bg-rose-100 text-rose-800",
  NO_RESPONSE: "bg-zinc-200 text-zinc-700",
  ESCALATED: "bg-red-100 text-red-800",
  WRITTEN_OFF: "bg-zinc-100 text-zinc-500",
};

export const MISSION_STYLE: Record<string, string> = {
  DRAFT: "bg-slate-100 text-slate-700",
  CLARIFYING: "bg-amber-100 text-amber-800",
  AWAITING_PLAN_APPROVAL: "bg-amber-100 text-amber-800",
  ACTIVE: "bg-sky-100 text-sky-800",
  PAUSED: "bg-orange-100 text-orange-800",
  ESCALATED: "bg-red-100 text-red-800",
  COMPLETED: "bg-emerald-100 text-emerald-800",
};

export function Pill({ text, style }: { text: string; style?: string }) {
  return <span className={`pill ${style ?? "bg-slate-100 text-slate-700"}`}>{text.replaceAll("_", " ").toLowerCase()}</span>;
}

export function Section({ title, right, children }: { title: string; right?: ReactNode; children: ReactNode }) {
  return (
    <section className="card">
      <div className="flex items-center gap-3 px-5 py-3 border-b border-line">
        <h2 className="font-semibold text-sm flex-1">{title}</h2>
        {right}
      </div>
      <div className="p-5">{children}</div>
    </section>
  );
}

export function Empty({ children }: { children: ReactNode }) {
  return <div className="text-sm text-muted py-6 text-center">{children}</div>;
}

export function MessageCard({ from, to, subject, body, attachments, meta, tone = "out" }: {
  from?: string; to: string; subject?: string | null; body: string; attachments?: string[]; meta?: ReactNode;
  tone?: "out" | "in";
}) {
  return (
    <div className={`rounded-lg border ${tone === "out" ? "border-teal-200 bg-teal-50/40" : "border-line bg-white"}`}>
      <div className="px-4 py-2 border-b border-line/70 text-xs text-muted flex flex-wrap gap-x-4 gap-y-1">
        {from && <span><b className="text-ink font-medium">From</b> {from}</span>}
        <span><b className="text-ink font-medium">To</b> {to}</span>
        {meta}
      </div>
      {subject && <div className="px-4 pt-3 font-medium text-sm">{subject}</div>}
      <div className="px-4 py-3 text-sm whitespace-pre-wrap leading-relaxed">{body}</div>
      {attachments && attachments.length > 0 && (
        <div className="px-4 pb-3 flex flex-wrap gap-2">
          {attachments.map((a) => <span key={a} className="pill bg-white border border-line text-slate-600">📎 {a.split("/").pop()}</span>)}
        </div>
      )}
    </div>
  );
}
