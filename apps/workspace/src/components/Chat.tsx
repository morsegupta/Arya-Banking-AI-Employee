import { useEffect, useRef, useState } from "react";
import type { Ctx } from "../App";
import { api, type Board, type ChatTurn, when } from "../api";
import { Empty } from "./ui";

const SUGGESTIONS = ["Who promised to pay this week?", "Why is Globex not paid yet?", "Are we going to hit the target?",
  "Which customers have not replied?"];

export default function Chat({ ctx }: { ctx: Ctx }) {
  const [turns, setTurns] = useState<ChatTurn[]>([]);
  const [board, setBoard] = useState<Board | null>(null);
  const [q, setQ] = useState("");
  const [asking, setAsking] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const end = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!ctx.missionId) return;
    api.mission(ctx.missionId).then((m) => setTurns(m.data.chat ?? []));
    api.board(ctx.missionId).then(setBoard);
  }, [ctx.missionId]);
  useEffect(() => { end.current?.scrollIntoView({ behavior: "smooth" }); }, [turns, asking]);

  if (!ctx.missionId) return <Empty>Start a mission first.</Empty>;
  const idOf = (ref: string) => board?.entities.find((e) => e.ref === ref)?.id;

  const ask = async (question: string) => {
    if (!question.trim()) return;
    setAsking(true); setErr(null); setQ("");
    try {
      const t = await api.chat(ctx.missionId!, question.trim(), ctx.reviewer || "manager");
      setTurns((x) => [...x, t]);
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setAsking(false);
    }
  };

  return (
    <div className="max-w-3xl flex flex-col h-full">
      <h1 className="text-xl font-semibold">Ask Arya</h1>
      <p className="text-sm text-muted mt-1">Answers come only from this mission's records; links open the invoice.</p>
      <div className="flex-1 mt-4 space-y-4">
        {turns.map((t, i) => (
          <div key={i} className="space-y-2">
            <div className="flex justify-end"><div className="rounded-2xl rounded-br-sm bg-brand text-white px-4 py-2 text-sm max-w-[80%]">{t.question}</div></div>
            <div className="card px-4 py-3 text-sm max-w-[90%]">
              <div className="whitespace-pre-wrap leading-relaxed">{t.answer}</div>
              {(t.refs.length > 0 || t.groups.length > 0) && (
                <div className="mt-2 flex flex-wrap gap-2">
                  {t.refs.map((r) => (
                    <button key={r} className="pill bg-slate-100 font-mono hover:bg-brand-soft"
                      onClick={() => { const id = idOf(r); if (id) ctx.openEntity(id); }}>{r}</button>
                  ))}
                  {t.groups.map((g) => <span key={g} className="pill bg-slate-50 border border-line">{g}</span>)}
                </div>
              )}
              <div className="mt-2 text-xs text-muted">{when(t.at)} · confidence {Math.round(t.confidence * 100)}%</div>
            </div>
          </div>
        ))}
        {asking && <div className="text-sm text-muted">Arya is checking the records…</div>}
        {err && <div className="text-sm text-rose-700">{err}</div>}
        <div ref={end} />
      </div>
      {turns.length === 0 && (
        <div className="flex flex-wrap gap-2 mt-4">
          {SUGGESTIONS.map((s) => <button key={s} className="btn-ghost" onClick={() => ask(s)}>{s}</button>)}
        </div>
      )}
      <form className="mt-4 flex gap-2 sticky bottom-0 bg-paper py-2" onSubmit={(e) => { e.preventDefault(); ask(q); }}>
        <input className="input" placeholder="Ask about promises, payments, customers…" value={q} onChange={(e) => setQ(e.target.value)} disabled={asking} />
        <button className="btn-primary" disabled={asking || !q.trim()}>Ask</button>
      </form>
    </div>
  );
}
