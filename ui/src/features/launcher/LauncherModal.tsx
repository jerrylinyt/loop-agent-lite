import { useEffect, useMemo, useState } from "react";
import Modal from "../../shared/components/Modal";
import { getJson, postJson } from "../../shared/api/client";
import type { ConfigResponse, JobInfo, WorkspaceSummary } from "../../shared/api/types";
import PlanImportField from "./PlanImportField";
import { validatePlan } from "./planValidation";

interface RepoStatus { goal: "committed" | "modified" | "untracked" | "missing"; tree_clean: boolean; error?: string }

export default function LauncherModal({
  workspaces,
  onClose,
  onLaunched
}: {
  workspaces: WorkspaceSummary[];
  onClose: () => void;
  onLaunched: (name: string) => void;
}) {
  const [tab, setTab] = useState<"launch" | "jobs">("launch");
  const [config, setConfig] = useState<ConfigResponse | null>(null);
  const [repoChoice, setRepoChoice] = useState("");
  const [customRepo, setCustomRepo] = useState("");
  const [repoStatus, setRepoStatus] = useState<RepoStatus | null>(null);
  const [goalFile, setGoalFile] = useState<File | null>(null);
  const [planJson, setPlanJson] = useState("");
  const [startPhase, setStartPhase] = useState<"plan" | "exec">("plan");
  const [name, setName] = useState("");
  const [agentIndex, setAgentIndex] = useState("0");
  const [validateChoice, setValidateChoice] = useState("0");
  const [customValidate, setCustomValidate] = useState("");
  const [flagThreshold, setFlagThreshold] = useState(10);
  const [doneThreshold, setDoneThreshold] = useState(3);
  const [roundTimeout, setRoundTimeout] = useState(30);
  const [resetState, setResetState] = useState(false);
  const [newBranch, setNewBranch] = useState(false);
  const [jobs, setJobs] = useState<JobInfo[]>([]);
  const [message, setMessage] = useState("");
  const [launching, setLaunching] = useState(false);

  const repo = repoChoice === "__custom__" ? customRepo.trim() : repoChoice;
  const matchingWorkspace = useMemo(() => workspaces.find((workspace) => workspace.repo === repo), [repo, workspaces]);

  useEffect(() => {
    void getJson<ConfigResponse>("/api/config").then((response) => {
      setConfig(response);
      if (!response) return setMessage("❌ 設定載入失敗");
      if (response.error) return setMessage(`❌ ${response.error}`);
      setRepoChoice(response.repos[0] ?? "__custom__");
      setFlagThreshold(response.defaults.flag_threshold ?? 10);
      setDoneThreshold(response.defaults.done_threshold ?? 3);
      setRoundTimeout(response.defaults.round_timeout ?? 30);
    });
  }, []);

  useEffect(() => {
    if (!repo) return setRepoStatus(null);
    let active = true;
    void getJson<RepoStatus>(`/api/repo-status?repo=${encodeURIComponent(repo)}`).then((status) => {
      if (active) setRepoStatus(status);
    });
    return () => { active = false; };
  }, [repo]);

  useEffect(() => {
    if (tab !== "jobs") return;
    const poll = () => void getJson<JobInfo[]>("/api/jobs").then((items) => items && setJobs(items));
    poll();
    const interval = window.setInterval(poll, 2000);
    return () => window.clearInterval(interval);
  }, [tab]);

  const launch = async () => {
    const planError = validatePlan(planJson);
    if (planError) return setMessage(`❌ plan.json 格式不對：${planError}`);
    setLaunching(true);
    setMessage("啟動中…");
    const body: Record<string, unknown> = {
      repo,
      name: name.trim(),
      agent_idx: +agentIndex,
      flag_threshold: flagThreshold,
      done_threshold: doneThreshold,
      round_timeout: roundTimeout,
      reset_state: resetState,
      new_branch: newBranch,
      plan_json: planJson,
      start_phase: startPhase
    };
    if (goalFile) body.goal_content = await goalFile.text();
    if (validateChoice === "__custom__") body.validate_custom = customValidate.trim();
    else body.validate_idx = +validateChoice;
    const response = await postJson<{ name?: string; pid?: number }>("/api/launch", body);
    setLaunching(false);
    if (response.error || !response.name) return setMessage(`❌ ${response.error ?? "啟動失敗"}`);
    setMessage(`✅ 已啟動 ${response.name}（pid ${response.pid}）`);
    onLaunched(response.name);
  };

  const stopJob = async (job: JobInfo) => {
    await postJson("/api/stop", { name: job.name });
    const items = await getJson<JobInfo[]>("/api/jobs");
    if (items) setJobs(items);
  };

  const repoMark = (value?: string) => value === "committed" ? "✅ 已 commit" : value === "modified" ? "⚠ 已修改未 commit" : value === "untracked" ? "⚠ 尚未 commit" : "❌ 缺少";
  const footer = tab === "launch" ? (
    <>
      <button type="button" className="secondary-button" onClick={onClose}>取消</button>
      <button type="button" className="primary-button" onClick={launch} disabled={launching || !repo}>▶ 啟動</button>
      <span className="inline-message" role="status">{message}</span>
    </>
  ) : <button type="button" className="secondary-button" onClick={onClose}>關閉</button>;

  return (
    <Modal title="啟動與管理" description="建立新 loop，或查看由這個 dashboard 啟動的工作" onClose={onClose} wide footer={footer}>
      <div className="segmented-tabs" role="tablist">
        <button type="button" role="tab" aria-selected={tab === "launch"} className={tab === "launch" ? "active" : ""} onClick={() => setTab("launch")} data-autofocus>啟動新 loop</button>
        <button type="button" role="tab" aria-selected={tab === "jobs"} className={tab === "jobs" ? "active" : ""} onClick={() => setTab("jobs")}>執行中的 jobs</button>
      </div>
      {tab === "launch" ? (
        <div className="form-grid launcher-form">
          <label>Repo
            <select value={repoChoice} onChange={(event) => setRepoChoice(event.target.value)}>
              {(config?.repos ?? []).map((item) => <option key={item} value={item}>{item}</option>)}
              <option value="__custom__">手動輸入…</option>
            </select>
          </label>
          {repoChoice === "__custom__" && <input value={customRepo} onChange={(event) => setCustomRepo(event.target.value)} placeholder="/path/to/repo" aria-label="Repo 路徑" />}
          {repoStatus && <div className={`repo-status${repoStatus.error || !repoStatus.tree_clean ? " warning" : ""}`}>
            {repoStatus.error ? `❌ ${repoStatus.error}` : <>goal.md {repoMark(repoStatus.goal)} · 工作樹 {repoStatus.tree_clean ? "✅ 乾淨" : "❌ 髒（preflight 會擋）"}{matchingWorkspace && ` · workspace「${matchingWorkspace.name}」已存在`}</>}
          </div>}
          <label>goal.md <span className="label-help">留空＝沿用 repo 已 commit 的版本</span>
            <input type="file" accept=".md,.markdown,.txt" onChange={(event) => setGoalFile(event.target.files?.[0] ?? null)} />
          </label>
          <PlanImportField value={planJson} onChange={setPlanJson} startPhase={startPhase} onStartPhaseChange={setStartPhase} />
          <label>Workspace 名稱 <span className="label-help">留空＝repo 目錄名</span><input value={name} onChange={(event) => setName(event.target.value)} /></label>
          <div className="form-columns">
            <label>Agent 命令<select value={agentIndex} onChange={(event) => setAgentIndex(event.target.value)}>{(config?.agent_cmds ?? []).map((agent, index) => <option key={agent.cmd} value={index}>{agent.label} — {agent.cmd}</option>)}</select></label>
            <label>Validate 命令<select value={validateChoice} onChange={(event) => setValidateChoice(event.target.value)}>{(config?.validate_cmds ?? []).map((command, index) => <option key={command.cmd} value={index}>{command.label} — {command.cmd}</option>)}<option value="__custom__">手寫…</option></select></label>
          </div>
          {validateChoice === "__custom__" && <input value={customValidate} onChange={(event) => setCustomValidate(event.target.value)} placeholder="mvn -q test" aria-label="自訂 Validate 命令" />}
          <details className="advanced-settings">
            <summary>進階設定</summary>
            <div className="number-grid">
              <label>flag 收斂（&gt;）<input type="number" min={1} value={flagThreshold} onChange={(event) => setFlagThreshold(+event.target.value)} /></label>
              <label>done 收斂（≥）<input type="number" min={1} value={doneThreshold} onChange={(event) => setDoneThreshold(+event.target.value)} /></label>
              <label>單輪上限（分）<input type="number" min={0} value={roundTimeout} onChange={(event) => setRoundTimeout(+event.target.value)} /></label>
            </div>
            <label className="checkbox-row"><input type="checkbox" checked={resetState} onChange={(event) => setResetState(event.target.checked)} />重置 workspace state（清除舊進度）</label>
            <label className="checkbox-row"><input type="checkbox" checked={newBranch} onChange={(event) => setNewBranch(event.target.checked)} />在新 branch 跑（loop/&lt;workspace 名&gt;）</label>
          </details>
        </div>
      ) : (
        <div className="jobs-list">
          {jobs.map((job) => <article className="job-card" key={job.name}>
            <div className="job-title"><div><strong>{job.name}</strong><span>pid {job.pid} · {job.alive ? "🟢 執行中" : `⚪ 已結束 rc=${job.rc}`}</span></div>{job.alive && <button type="button" className="danger-button" onClick={() => stopJob(job)}>⏹ 停止</button>}</div>
            <div className="job-repo">{job.repo}</div><pre>{job.tail || "尚無輸出"}</pre>
          </article>)}
          {!jobs.length && <div className="empty-inline">沒有由這個 dashboard 啟動的 job</div>}
          <p className="modal-note">關閉 dashboard 會停止上面仍在執行的 loop；state 已落地，可重新啟動續跑。</p>
        </div>
      )}
    </Modal>
  );
}
