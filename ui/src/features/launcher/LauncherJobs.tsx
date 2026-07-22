/** Dashboard 啟動的 jobs 清單：自行管理輪詢與停止後刷新，不讓啟動表單持有其生命週期。 */
import { useCallback, useEffect, useRef, useState } from "react";
import { getJson, postJobActionAndWait } from "../../shared/api/client";
import type { JobInfo } from "../../shared/api/types";

interface JobActionMessage {
  text: string;
  error: boolean;
}

function jobKey(job: JobInfo) {
  return job.id ?? `${job.name}:${job.pid}`;
}

export default function LauncherJobs() {
  const [jobs, setJobs] = useState<JobInfo[]>([]);
  const [busyJob, setBusyJob] = useState<string | null>(null);
  const [actionMessage, setActionMessage] = useState<JobActionMessage | null>(null);
  const mounted = useRef(true);
  const jobsRequestSequence = useRef(0);

  const refreshJobs = useCallback(async () => {
    const requestSequence = ++jobsRequestSequence.current;
    const items = await getJson<JobInfo[]>("/api/jobs");
    if (mounted.current && requestSequence === jobsRequestSequence.current && items) {
      setJobs(items);
    }
  }, []);

  useEffect(() => {
    mounted.current = true;
    let cancelled = false;
    let timeout: number | undefined;
    const poll = async () => {
      await refreshJobs();
      if (!cancelled) timeout = window.setTimeout(() => void poll(), 2000);
    };
    void poll();
    return () => {
      cancelled = true;
      mounted.current = false;
      jobsRequestSequence.current += 1;
      if (timeout !== undefined) window.clearTimeout(timeout);
    };
  }, [refreshJobs]);

  const stopJob = async (job: JobInfo) => {
    const key = jobKey(job);
    if (busyJob !== null) return;
    const parallelSupervisor = job.kind === "parallel-supervisor";
    const actionLabel = parallelSupervisor ? "Pause" : "停止";
    setBusyJob(key);
    setActionMessage({ text: `${job.name} ${actionLabel} 處理中…`, error: false });
    try {
      const result = await postJobActionAndWait("/api/stop", { name: job.name }, job.name);
      if (!mounted.current) return;
      if (result.error) {
        setActionMessage({ text: `${job.name} ${actionLabel} 失敗：${result.error}`, error: true });
        return;
      }
      setActionMessage({ text: `${job.name} ${actionLabel} 完成`, error: false });
      await refreshJobs();
    } finally {
      if (mounted.current) setBusyJob(null);
    }
  };

  return (
    <div className="jobs-list">
      {jobs.map((job) => {
        const parallelControl = job.kind?.startsWith("parallel-") && job.kind.endsWith("-control");
        const parallelSupervisor = job.kind === "parallel-supervisor";
        const key = jobKey(job);
        const busy = busyJob === key;
        const actionLabel = parallelSupervisor ? "Pause" : "停止";
        return <article className="job-card" key={key}>
        <div className="job-title"><div><strong>{job.name}</strong><span>pid {job.pid} · {job.kind ?? "runner"} · {job.alive ? "執行中" : `已結束 rc=${job.rc}`}</span></div>{job.alive && !parallelControl && <button type="button" className={parallelSupervisor ? "secondary-button" : "danger-button"} disabled={busyJob !== null} onClick={() => void stopJob(job)}>{busy ? `${actionLabel} 中…` : actionLabel}</button>}</div>
        <div className="job-repo">{job.repo}</div><pre>{job.tail || "尚無輸出"}</pre>
      </article>;
      })}
      {!jobs.length && <div className="empty-inline">沒有由這個 dashboard 啟動的 job</div>}
      {actionMessage && <p
        className={actionMessage.error ? "field-error" : "inline-message"}
        role={actionMessage.error ? "alert" : "status"}
        aria-live={actionMessage.error ? "assertive" : "polite"}
      >{actionMessage.text}</p>}
      <p className="modal-note">關閉 dashboard 會停止上面仍在執行的 loop；state 已落地，可重新啟動續跑。</p>
    </div>
  );
}
