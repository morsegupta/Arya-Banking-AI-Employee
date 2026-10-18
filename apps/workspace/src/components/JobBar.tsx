import { useState } from "react";
import type { Job } from "../api";

export default function JobBar({ job }: { job: Job | null }) {
  const [open, setOpen] = useState(false);
  if (!job) return null;
  const running = job.status === "running";
  const failed = job.status === "failed";
  const last = job.log[job.log.length - 1];
  return (
    <div className={`mx-6 mt-4 rounded-lg border px-4 py-2 text-sm ${failed ? "border-rose-200 bg-rose-50" : running ? "border-sky-200 bg-sky-50" : "border-line bg-white"}`}>
      <div className="flex items-center gap-3">
        {running ? <span className="h-2 w-2 rounded-full bg-sky-500 animate-pulse" /> :
          <span className={`h-2 w-2 rounded-full ${failed ? "bg-rose-500" : "bg-emerald-500"}`} />}
        <span className="font-medium">{job.name}</span>
        <span className="text-muted">{running ? "working…" : job.status}</span>
        <span className="flex-1 truncate text-muted">{last}</span>
        {job.log.length > 0 && <button className="text-xs text-brand" onClick={() => setOpen(!open)}>{open ? "hide log" : `log (${job.log.length})`}</button>}
      </div>
      {open && (
        <pre className="mt-2 max-h-64 overflow-auto rounded bg-slate-900 text-slate-100 text-xs p-3 whitespace-pre-wrap">{job.log.join("\n")}</pre>
      )}
    </div>
  );
}
