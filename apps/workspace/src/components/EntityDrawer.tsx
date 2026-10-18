import { useEffect, useState } from "react";
import { api, type EntityDetail, inr, when } from "../api";
import { Empty, MessageCard, Pill, STATE_STYLE } from "./ui";

type Tab = "timeline" | "conversation" | "evidence";

export default function EntityDrawer({ id, refresh, onClose }: { id: string; refresh: number; onClose: () => void }) {
  const [e, setE] = useState<EntityDetail | null>(null);
  const [tab, setTab] = useState<Tab>("timeline");

  useEffect(() => { api.entity(id).then(setE).catch(() => setE(null)); }, [id, refresh]);
  useEffect(() => {
    const onKey = (ev: KeyboardEvent) => ev.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  return (
    <div className="fixed inset-0 z-20 flex">
      <div className="flex-1 bg-slate-900/20" onClick={onClose} />
      <div className="w-[640px] max-w-full bg-white border-l border-line flex flex-col shadow-xl">
        {!e ? <Empty>Loading…</Empty> : (
          <>
            <div className="px-6 py-5 border-b border-line">
              <div className="flex items-center gap-3">
                <span className="font-mono font-semibold">{e.ref}</span>
                <Pill text={e.state} style={STATE_STYLE[e.state]} />
                {e.quarantined && <span className="pill bg-red-100 text-red-700">quarantined</span>}
                <span className="flex-1" />
                <button className="text-muted hover:text-ink" onClick={onClose}>✕</button>
              </div>
              <div className="mt-2 text-lg font-semibold">{e.customer}</div>
              <div className="mt-3 grid grid-cols-4 gap-3 text-sm">
                <div><div className="label">Balance</div><div className="font-semibold tabular-nums">{inr(e.balance)}</div></div>
                <div><div className="label">Invoice</div><div className="tabular-nums">{inr(e.value)}</div></div>
                <div><div className="label">Due</div><div>{e.due_date} <span className="text-muted">({e.days_overdue}d)</span></div></div>
                <div><div className="label">Promise</div><div>{e.promise_date ? `${e.promise_date}` : "—"}</div></div>
              </div>
              <div className="mt-3 text-xs text-muted">Contact: {e.contact.name} · {e.contact.email} · {e.contact.whatsapp}</div>
            </div>
            <div className="px-6 border-b border-line flex gap-4 text-sm">
              {(["timeline", "conversation", "evidence"] as Tab[]).map((t) => (
                <button key={t} onClick={() => setTab(t)}
                  className={`py-3 border-b-2 -mb-px capitalize ${tab === t ? "border-brand text-ink font-medium" : "border-transparent text-muted"}`}>
                  {t}{t === "conversation" ? ` (${e.thread.length})` : t === "evidence" ? ` (${e.evidence.length})` : ""}
                </button>
              ))}
            </div>
            <div className="flex-1 overflow-auto p-6">
              {tab === "timeline" && (
                <ol className="relative border-l border-line ml-2 space-y-4">
                  {e.timeline.map((t) => (
                    <li key={t.seq} className="ml-4">
                      <div className={`absolute -left-1.5 mt-1.5 h-3 w-3 rounded-full border-2 border-white ${
                        t.type === "entity.transitioned" ? "bg-brand" : t.type.startsWith("security") ? "bg-red-500" : "bg-slate-300"}`} />
                      <div className="text-xs text-muted">{when(t.at)} · {t.type}</div>
                      <div className="text-sm">{t.text}</div>
                    </li>
                  ))}
                </ol>
              )}
              {tab === "conversation" && (e.thread.length ? (
                <div className="space-y-3">
                  {e.thread.map((msg) => (
                    <MessageCard key={msg.id} tone={msg.outbound ? "out" : "in"}
                      from={msg.from_addr} to={msg.to_addr} subject={msg.subject} body={msg.body}
                      attachments={msg.attachments?.map((a) => a.filename)}
                      meta={<span>{when(msg.sent_at)} · {msg.channel} · {msg.status}</span>} />
                  ))}
                </div>
              ) : <Empty>No messages yet.</Empty>)}
              {tab === "evidence" && (e.evidence.length ? (
                <ul className="space-y-2 text-sm">
                  {e.evidence.map((ev) => (
                    <li key={ev.id} className="card p-3">
                      <div className="flex gap-2 items-center">
                        <Pill text={ev.subject} />
                        <span className="text-xs text-muted">{ev.kind}</span>
                        <span className="flex-1" />
                        {ev.sha256 && <span className="text-xs text-emerald-700" title={ev.sha256}>sha256 kept</span>}
                      </div>
                      <div className="mt-1">{ev.summary}</div>
                      <div className="mt-1 font-mono text-xs text-muted break-all">{ev.ref}</div>
                    </li>
                  ))}
                </ul>
              ) : <Empty>No evidence yet.</Empty>)}
            </div>
          </>
        )}
      </div>
    </div>
  );
}
