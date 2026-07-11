/** Dashboard 啟動的 jobs 清單：自行管理輪詢與停止後刷新，不讓啟動表單持有其生命週期。 */
import { useEffect, useState } from "react";
import { getJson, postJson } from "../../shared/api/client";
import type { JobInfo } from "../../shared/api/types";

export default function LauncherJobs() {
  const [jobs, setJobs] = useState<JobInfo[]>([]);

  useEffect(() => {
    let active = true;
    const poll = async () => {
      const items = await getJson<JobInfo[]>("/api/jobs");
      if (active && items) setJobs(items);
    };
    void poll();
    const interval = window.setInterval(() => void poll(), 2000);
    return () => {
      active = false;
      window.clearInterval(interval);
    };
  }, []);

  const stopJob = async (job: JobInfo) => {
    await postJson("/api/stop", { name: job.name });
    const items = await getJson<JobInfo[]>("/api/jobs");
    if (items) setJobs(items);
  };

  return (
    <div className="jobs-list">
      {jobs.map((job) => <article className="job-card" key={job.name}>
        <div className="job-title"><div><strong>{job.name}</strong><span>pid {job.pid} · {job.alive ? "🟢 執行中" : `⚪ 已結束 rc=${job.rc}`}</span></div>{job.alive && <button type="button" className="danger-button" onClick={() => void stopJob(job)}>⏹ 停止</button>}</div>
        <div className="job-repo">{job.repo}</div><pre>{job.tail || "尚無輸出"}</pre>
      </article>)}
      {!jobs.length && <div className="empty-inline">沒有由這個 dashboard 啟動的 job</div>}
      <p className="modal-note">關閉 dashboard 會停止上面仍在執行的 loop；state 已落地，可重新啟動續跑。</p>
    </div>
  );
}
