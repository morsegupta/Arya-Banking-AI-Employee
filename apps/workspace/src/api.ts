// Typed client for the workspace API (apps/api.py). Money is integer paise everywhere.

export type Job = { id: string; name: string; status: "running" | "done" | "failed"; log: string[]; result?: unknown; error?: string | null };

export type Status = {
  now: string; company: string; employee: string; role: string;
  missions: { id: string; brief: string; status: string }[];
  pending_approvals: number; job: Job | null; sandbox_sim: boolean;
};

export type Progress = {
  target: number | null; verified: number; open_by_state: Record<string, number>;
  expected: number; gap: number | null; expected_shortfall: number | null;
};

export type Draft = { channel: string; to: string; subject?: string | null; body: string; attachments: string[]; refs: string[]; approach: string };

export type Mission = {
  id: string; brief: string; status: string; created_at: string;
  spec: null | { objective: string; goal?: string | null; scope: string; target_value: number | null;
    target_unit?: string | null; deadline: string | null;
    success_criteria: string[]; assumptions: string[]; open_questions: string[] };
  plan: null | { summary: string; batches: { name: string; groups: string[]; approach: string; start_day: number; rationale: string }[];
    expected_value: number | null; risks: string[]; human_asks: string[]; problems: string[] };
  selection: Record<string, unknown>;
  data: { excluded?: { ref: string; reason: string }[]; groups?: Record<string, { name: string; refs: string[]; score: number; suggested_approach: string }>;
    scope_value?: number; first_batch?: Record<string, Draft>; contacted_groups?: string[]; activated_on?: string;
    chat?: ChatTurn[]; actions?: { date: string; lever: string; groups: string[]; result: string }[];
    prepaid?: { ref: string; record_id: string; amount: number; kind: string }[] };
  progress: Progress | null; plan_text: string[]; budget: { used: number; max: number };
};

export type EntityRow = {
  id: string; ref: string; state: string; group: string; customer: string; balance: number; value: number;
  days_overdue: number; due_date: string; promise_date?: string | null; promise_amount?: number | null;
  last_contact?: string | null; quarantined: boolean; escalation_reason?: string | null;
};

export type Board = { states: string[]; terminal: string[]; entities: EntityRow[] };

export type FeedEvent = { seq: number; type: string; at: string; mission_id: string | null; entity_id: string | null; ref?: string | null; text: string };

export type EntityDetail = EntityRow & {
  mission_id: string; record: Record<string, unknown>; contact: Record<string, string>; facts: Record<string, unknown>;
  documents: string[]; timeline: FeedEvent[];
  evidence: { id: string; subject: string; kind: string; ref: string; summary: string; sha256: string | null; at: string }[];
  thread: { id: string; from_addr: string; to_addr: string; subject?: string; body: string; sent_at: string; status: string; channel: string;
    attachments?: { filename: string }[]; outbound: boolean }[];
};

export type Approval = {
  id: string; kind: string; title: string; status: string; requested_at: string; decided_by?: string | null; note?: string | null;
  mission_id?: string | null; entity_id?: string | null; ref?: string | null; payload: Record<string, any>;
};

export type MemoryItem = {
  id: string; scope: string; kind: "fact" | "lesson" | "playbook"; key: string; text: string; status: string;
  source?: string | null; decided_by?: string | null; updated_at: string; cites: string[];
};

export type ChatTurn = { at: string; by: string; question: string; answer: string; refs: string[]; groups: string[]; confidence: number };

async function req<T>(path: string, init?: RequestInit): Promise<T> {
  // X-Arya-Workspace: the API refuses actions without it, so other sites can't drive it via this browser
  const res = await fetch(path, { headers: { "Content-Type": "application/json", "X-Arya-Workspace": "1" }, ...init });
  if (!res.ok) {
    let detail = res.statusText;
    try { detail = (await res.json()).detail ?? detail; } catch { /* not json */ }
    throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
  }
  return res.json() as Promise<T>;
}

export const api = {
  status: () => req<Status>("/api/status"),
  mission: (id: string) => req<Mission>(`/api/missions/${id}`),
  board: (id: string) => req<Board>(`/api/missions/${id}/board`),
  entity: (id: string) => req<EntityDetail>(`/api/entities/${id}`),
  events: (mission?: string) => req<FeedEvent[]>(`/api/events?limit=300${mission ? `&mission_id=${mission}` : ""}`),
  approvals: (status = "PENDING") => req<Approval[]>(`/api/approvals?status=${status}`),
  createMission: (brief: string) => req<Job>("/api/missions", { method: "POST", body: JSON.stringify({ brief }) }),
  decide: (id: string, approve: boolean, by: string, note: string) =>
    req<Job>(`/api/approvals/${id}`, { method: "POST", body: JSON.stringify({ approve, by, note }) }),
  tick: (id: string) => req<Job>(`/api/missions/${id}/tick`, { method: "POST" }),
  day: (id: string) => req<Job>(`/api/missions/${id}/day`, { method: "POST" }),
  resume: (id: string, by: string, extra_tokens: number) =>
    req<Job>(`/api/missions/${id}/resume`, { method: "POST", body: JSON.stringify({ by, extra_tokens }) }),
  memory: (all = false) => req<MemoryItem[]>(`/api/memory${all ? "?all=true" : ""}`),
  decideMemory: (id: string, status: "pinned" | "rejected" | "active", by: string) =>
    req<MemoryItem>(`/api/memory/${id}`, { method: "POST", body: JSON.stringify({ status, by }) }),
  learn: (id: string) => req<Job>(`/api/missions/${id}/learn`, { method: "POST" }),
  chat: (id: string, question: string, by: string) =>
    req<ChatTurn>(`/api/missions/${id}/chat`, { method: "POST", body: JSON.stringify({ question, by }) }),
};

// ₹ with Indian digit grouping, from paise.
export function inr(paise: number | null | undefined, decimals = false): string {
  if (paise === null || paise === undefined) return "—";
  const rupees = paise / 100;
  return "₹" + rupees.toLocaleString("en-IN", { minimumFractionDigits: decimals ? 2 : 0, maximumFractionDigits: decimals ? 2 : 0 });
}

// "₹12.4L" style for headline numbers.
export function inrShort(paise: number | null | undefined): string {
  if (paise === null || paise === undefined) return "—";
  const r = paise / 100;
  if (r >= 1e7) return `₹${(r / 1e7).toFixed(2)}Cr`;
  if (r >= 1e5) return `₹${(r / 1e5).toFixed(2)}L`;
  return inr(paise);
}

export function when(iso: string): string {
  const d = new Date(iso);
  return d.toLocaleString("en-IN", { weekday: "short", day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit", hour12: false });
}
