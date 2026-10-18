import { useCallback, useEffect, useRef, useState } from "react";
import { api, type FeedEvent, type Job, type Status, when } from "./api";
import Home from "./components/Home";
import MissionView from "./components/MissionView";
import BoardView from "./components/BoardView";
import Approvals from "./components/Approvals";
import Chat from "./components/Chat";
import Activity from "./components/Activity";
import EntityDrawer from "./components/EntityDrawer";
import JobBar from "./components/JobBar";

export type View = "home" | "mission" | "board" | "approvals" | "chat" | "activity";

const NAV: { id: View; label: string; icon: string }[] = [
  { id: "home", label: "Home", icon: "◎" },
  { id: "mission", label: "Mission & plan", icon: "◇" },
  { id: "board", label: "Invoices", icon: "▦" },
  { id: "approvals", label: "Approvals", icon: "✓" },
  { id: "chat", label: "Ask Arya", icon: "✦" },
  { id: "activity", label: "Activity", icon: "≡" },
];

export type Ctx = {
  missionId: string | null;
  refresh: number;
  openEntity: (id: string) => void;
  go: (v: View) => void;
  startJob: (p: Promise<Job>) => Promise<void>;
  job: Job | null;
  reviewer: string;
  setReviewer: (s: string) => void;
  feed: FeedEvent[];
  status: Status | null;
  setMissionId: (id: string) => void;
};

function loadReviewer(): string {
  try { return localStorage.getItem("arya.reviewer") || ""; } catch { return ""; }
}

export default function App() {
  const [view, setView] = useState<View>("home");
  const [status, setStatus] = useState<Status | null>(null);
  const [missionId, setMissionId] = useState<string | null>(null);
  const [refresh, setRefresh] = useState(0);
  const [drawer, setDrawer] = useState<string | null>(null);
  const [job, setJob] = useState<Job | null>(null);
  const [feed, setFeed] = useState<FeedEvent[]>([]);
  const [reviewer, setReviewerState] = useState(loadReviewer());
  const [error, setError] = useState<string | null>(null);
  const bumpTimer = useRef<number | null>(null);

  const bump = useCallback(() => {
    if (bumpTimer.current) window.clearTimeout(bumpTimer.current);
    bumpTimer.current = window.setTimeout(() => setRefresh((n) => n + 1), 400);
  }, []);

  const setReviewer = (s: string) => {
    setReviewerState(s);
    try { localStorage.setItem("arya.reviewer", s); } catch { /* private window */ }
  };

  // status (missions, clock, pending approvals) follows every refresh
  useEffect(() => {
    api.status().then((s) => {
      setStatus(s);
      setJob(s.job);
      setMissionId((cur) => cur ?? (s.missions.length ? s.missions[s.missions.length - 1].id : null));
    }).catch((e) => setError(String(e)));
  }, [refresh]);

  // initial feed, then live updates over SSE
  useEffect(() => {
    let since = 0;
    let es: EventSource | null = null;
    api.events().then((evs) => {
      setFeed(evs.slice(-300));
      since = evs.length ? evs[evs.length - 1].seq : 0;
      es = new EventSource(`/api/stream?since=${since}`);
      es.addEventListener("event", (m) => {
        const ev = JSON.parse((m as MessageEvent).data) as FeedEvent;
        setFeed((f) => [...f.slice(-299), ev]);
        bump();
      });
      es.addEventListener("job", (m) => {
        const j = JSON.parse((m as MessageEvent).data) as Job;
        setJob(j);
        bump();
      });
    }).catch((e) => setError(String(e)));
    return () => es?.close();
  }, [bump]);

  const startJob = async (p: Promise<Job>) => {
    setError(null);
    try {
      setJob(await p);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  };

  const ctx: Ctx = {
    missionId, refresh, openEntity: setDrawer, go: setView, startJob, job, reviewer, setReviewer, feed, status,
    setMissionId: (id) => { setMissionId(id); setRefresh((n) => n + 1); },
  };
  const pending = status?.pending_approvals ?? 0;

  return (
    <div className="flex h-full">
      <aside className="w-60 shrink-0 border-r border-line bg-white flex flex-col">
        <div className="px-5 py-5 border-b border-line">
          <div className="flex items-center gap-2">
            <div className="h-8 w-8 rounded-lg bg-brand text-white grid place-items-center font-semibold">A</div>
            <div>
              <div className="font-semibold leading-tight">Arya</div>
              <div className="text-xs text-muted leading-tight">Accounts receivable</div>
            </div>
          </div>
        </div>
        <nav className="p-3 flex-1 space-y-0.5">
          {NAV.map((n) => (
            <button key={n.id} onClick={() => setView(n.id)}
              className={`w-full flex items-center gap-3 rounded-lg px-3 py-2 text-sm text-left transition ${
                view === n.id ? "bg-brand-soft text-teal-900 font-medium" : "text-slate-600 hover:bg-slate-50"}`}>
              <span className="w-4 text-center opacity-70">{n.icon}</span>
              <span className="flex-1">{n.label}</span>
              {n.id === "approvals" && pending > 0 && (
                <span className="pill bg-amber-100 text-amber-800">{pending}</span>
              )}
            </button>
          ))}
        </nav>
        <div className="p-4 border-t border-line text-xs text-muted space-y-1">
          <div className="font-medium text-ink">{status?.company ?? "…"}</div>
          <div>Sandbox time: {status ? when(status.now) : "…"}</div>
        </div>
      </aside>

      <main className="flex-1 min-w-0 flex flex-col">
        <header className="h-14 shrink-0 border-b border-line bg-white px-6 flex items-center gap-4">
          {status && status.missions.length > 1 ? (
            <select className="input max-w-md" value={missionId ?? ""} onChange={(e) => ctx.setMissionId(e.target.value)}>
              {status.missions.map((m) => <option key={m.id} value={m.id}>{m.brief.slice(0, 70)}</option>)}
            </select>
          ) : (
            <div className="text-sm text-muted truncate">{status?.missions.find((m) => m.id === missionId)?.brief ?? "No mission yet"}</div>
          )}
          <div className="flex-1" />
          <label className="text-xs text-muted flex items-center gap-2">
            You are
            <input className="input w-44 py-1" placeholder="your name" value={reviewer}
              onChange={(e) => setReviewer(e.target.value)} />
          </label>
        </header>
        {error && (
          <div className="mx-6 mt-4 rounded-lg border border-rose-200 bg-rose-50 px-4 py-2 text-sm text-rose-800 flex">
            <span className="flex-1">{error}</span>
            <button className="text-rose-600" onClick={() => setError(null)}>dismiss</button>
          </div>
        )}
        <JobBar job={job} />
        <div className="flex-1 overflow-auto p-6">
          {view === "home" && <Home ctx={ctx} />}
          {view === "mission" && <MissionView ctx={ctx} />}
          {view === "board" && <BoardView ctx={ctx} />}
          {view === "approvals" && <Approvals ctx={ctx} />}
          {view === "chat" && <Chat ctx={ctx} />}
          {view === "activity" && <Activity ctx={ctx} />}
        </div>
      </main>
      {drawer && <EntityDrawer id={drawer} refresh={refresh} onClose={() => setDrawer(null)} />}
    </div>
  );
}
