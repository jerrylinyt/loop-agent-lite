/** Dashboard 啟動的 jobs 清單：自行管理輪詢與停止後刷新，不讓啟動表單持有其生命週期。 */
import { useCallback, useEffect, useRef, useState } from "react";
import { getJson, postJson } from "../../shared/api/client";
import type { JobInfo } from "../../shared/api/types";
import type { BeginOperation, EndOperation } from "../../shared/operationGate";

function jobIdentity(job: JobInfo): string {
  return JSON.stringify([job.name, job.pid, job.kind, job.run_id ?? null, job.workspace_generation ?? null]);
}

export default function LauncherJobs({ operationPending, beginOperation, endOperation }: {
  operationPending: boolean;
  beginOperation: BeginOperation;
  endOperation: EndOperation;
}) {
  const [jobs, setJobs] = useState<JobInfo[]>([]);
  const [stopping, setStopping] = useState<Set<string>>(() => new Set());
  const [messages, setMessages] = useState<Record<string, string>>({});
  const stopPending = useRef(new Set<string>());
  const mounted = useRef(false);
  const jobsRevision = useRef(0);

  const refreshJobs = useCallback(async () => {
    // Interval polling and the explicit post-stop refresh share one epoch. A request that
    // started against an older job snapshot must not overwrite a newer replacement/stopped row.
    const revision = ++jobsRevision.current;
    const items = await getJson<JobInfo[]>("/api/jobs");
    if (mounted.current && revision === jobsRevision.current && items) setJobs(items);
  }, []);

  useEffect(() => {
    mounted.current = true;
    void refreshJobs();
    const interval = window.setInterval(() => void refreshJobs(), 2000);
    return () => {
      mounted.current = false;
      jobsRevision.current += 1;
      window.clearInterval(interval);
    };
  }, [refreshJobs]);

  const stopJob = async (job: JobInfo) => {
    const identity = jobIdentity(job);
    if (stopPending.current.has(identity)) return;
    const token = beginOperation(`launcher-job:${identity}:stop`);
    if (!token) return;
    stopPending.current.add(identity);
    setStopping((current) => new Set(current).add(identity));
    setMessages((current) => ({ ...current, [identity]: "停止請求送出中…" }));
    try {
      const response = await postJson<{ requested?: boolean; graceful?: boolean }>("/api/stop", {
        name: job.name,
        expected_pid: job.pid,
        ...(job.run_id ? { run_id: job.run_id } : {}),
        ...(job.workspace_generation ? { workspace_generation: job.workspace_generation } : {})
      });
      setMessages((current) => ({
        ...current,
        [identity]: response.error ? `❌ ${response.error}`
          : response.graceful || response.requested ? "✅ 已要求完成 active round 後停止"
            : "✅ 已停止"
      }));
      await refreshJobs();
    } finally {
      stopPending.current.delete(identity);
      setStopping((current) => {
        const next = new Set(current);
        next.delete(identity);
        return next;
      });
      endOperation(token);
    }
  };

  return (
    <div className="jobs-list">
      {jobs.map((job) => {
        const identity = jobIdentity(job);
        return <article className="job-card" key={identity}>
        <div className="job-title"><div><strong>{job.name}</strong><span>{job.kind === "fleet" ? "Parallel fleet" : "Standalone loop"} · pid {job.pid} · {job.alive ? "🟢 執行中" : `⚪ 已結束 rc=${job.rc}`}</span></div>{job.alive && <button type="button" className="danger-button" disabled={operationPending || stopping.has(identity)} onClick={() => void stopJob(job)}>{stopping.has(identity) ? "停止請求中…" : "⏹ 停止"}</button>}</div>
        {messages[identity] && <p className="job-action-message" role="status">{messages[identity]}</p>}
        <div className="job-repo">{job.repo}</div><pre>{job.tail || "尚無輸出"}</pre>
      </article>})}
      {!jobs.length && <div className="empty-inline">沒有由這個 dashboard 啟動的 job</div>}
      <p className="modal-note">關閉 dashboard 會停止上面仍在執行的 loop；state 已落地，可重新啟動續跑。</p>
    </div>
  );
}
