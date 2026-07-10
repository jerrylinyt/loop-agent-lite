import { useEffect, useMemo, useRef, useState } from "react";
import CliManagerModal from "../cli/CliManagerModal";
import Modal from "../../shared/components/Modal";
import { getJson, postJson, waitForJobStartup } from "../../shared/api/client";
import type { ConfigResponse, JobInfo, StartupResponse, WorkspaceState, WorkspaceSummary } from "../../shared/api/types";
import PlanImportField from "./PlanImportField";
import RepoRootsModal from "./RepoRootsModal";
import { validatePlan } from "./planValidation";

interface RepoStatus { goal: "committed" | "modified" | "untracked" | "missing"; tree_clean: boolean; suggested_validate_cmd?: string | null; error?: string }
interface ValidateResponse { ok?: boolean; rc?: number; timeout?: boolean; timeout_seconds?: number; tail?: string }

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
  const [agentBackoffMax, setAgentBackoffMax] = useState(60);
  const [validateTimeout, setValidateTimeout] = useState(120);
  const [resetState, setResetState] = useState(false);
  const [newBranch, setNewBranch] = useState(false);
  const [jobs, setJobs] = useState<JobInfo[]>([]);
  const [message, setMessage] = useState("");
  const [launching, setLaunching] = useState(false);
  const [validating, setValidating] = useState(false);
  const [validateResult, setValidateResult] = useState<{ ok: boolean; text: string; tail: string } | null>(null);
  const [cliManagerOpen, setCliManagerOpen] = useState(false);
  const [repoRootsOpen, setRepoRootsOpen] = useState(false);
  const hydratedRepo = useRef("");

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
      setAgentBackoffMax(response.defaults.agent_backoff_max ?? 60);
      setValidateTimeout(response.defaults.validate_timeout ?? 120);
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
    if (!config || !repo || !repoStatus || hydratedRepo.current === repo) return;
    hydratedRepo.current = repo;
    let active = true;

    const selectValidate = (command?: string | null) => {
      if (!command) return;
      const index = config.validate_cmds.findIndex((item) => item.cmd === command);
      if (index >= 0) {
        setValidateChoice(String(index));
        setCustomValidate("");
      } else {
        setValidateChoice("__custom__");
        setCustomValidate(command);
      }
      setValidateResult(null);
    };

    void (async () => {
      if (matchingWorkspace) {
        const existing = await getJson<WorkspaceState>(`/api/state?ws=${encodeURIComponent(matchingWorkspace.name)}`);
        if (!active) return;
        if (existing && !existing.error) {
          const saved = existing.config ?? {};
          const agent = config.agent_cmds.findIndex((item) => item.cmd === saved.agent_cmd);
          if (agent >= 0) setAgentIndex(String(agent));
          selectValidate(saved.validate_cmd);
          setName(matchingWorkspace.name);
          setFlagThreshold(saved.flag_threshold ?? config.defaults.flag_threshold ?? 10);
          setDoneThreshold(saved.done_threshold ?? config.defaults.done_threshold ?? 3);
          setRoundTimeout(saved.round_timeout ?? config.defaults.round_timeout ?? 30);
          setAgentBackoffMax(saved.agent_backoff_max ?? config.defaults.agent_backoff_max ?? 60);
          setValidateTimeout(saved.validate_timeout ?? config.defaults.validate_timeout ?? 120);
          return;
        }
      }
      selectValidate(repoStatus.suggested_validate_cmd);
    })();
    return () => { active = false; };
  }, [config, matchingWorkspace, repo, repoStatus]);

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
      agent_backoff_max: agentBackoffMax,
      validate_timeout: validateTimeout,
      reset_state: resetState,
      new_branch: newBranch,
      plan_json: planJson,
      start_phase: startPhase
    };
    if (goalFile) body.goal_content = await goalFile.text();
    if (validateChoice === "__custom__") body.validate_custom = customValidate.trim();
    else body.validate_idx = +validateChoice;
    const response = await postJson<StartupResponse>("/api/launch", body);
    if (response.error || !response.name || !response.pid) {
      setLaunching(false);
      return setMessage(`❌ ${response.error ?? "啟動失敗"}`);
    }
    if (response.starting) {
      setMessage("啟動前檢查中…");
      const startup = await waitForJobStartup(response.name, response.pid, response.startup_timeout ?? validateTimeout + 15);
      if (startup.error) {
        setLaunching(false);
        return setMessage(`❌ ${startup.error}`);
      }
    }
    setLaunching(false);
    setMessage(`✅ 已啟動 ${response.name}（pid ${response.pid}）`);
    onLaunched(response.name);
  };

  const stopJob = async (job: JobInfo) => {
    await postJson("/api/stop", { name: job.name });
    const items = await getJson<JobInfo[]>("/api/jobs");
    if (items) setJobs(items);
  };

  const verifyValidate = async () => {
    const validateCmd = validateChoice === "__custom__"
      ? customValidate.trim()
      : config?.validate_cmds[+validateChoice]?.cmd ?? "";
    setValidating(true);
    setValidateResult(null);
    const response = await postJson<ValidateResponse>("/api/validate", { repo, validate_cmd: validateCmd, validate_timeout: validateTimeout });
    setValidating(false);
    if (response.error) {
      setValidateResult({ ok: false, text: `❌ ${response.error}`, tail: "" });
      return;
    }
    setValidateResult({
      ok: !!response.ok,
      text: response.timeout ? `❌ 執行逾時（${response.timeout_seconds ?? validateTimeout} 秒）` : response.ok ? "✅ Validate 通過（exit 0）" : `❌ Validate 失敗（exit ${response.rc ?? "?"}）`,
      tail: response.tail ?? ""
    });
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
          <div className="form-field repo-select-field"><span>Repo</span><div className="command-select-row"><select aria-label="Repo" value={repoChoice} onChange={(event) => setRepoChoice(event.target.value)}>
                {(config?.repos ?? []).map((item) => <option key={item} value={item}>{item}</option>)}
                <option value="__custom__">手動輸入…</option>
              </select><button type="button" className="icon-button cli-gear-button" aria-label="管理 Code Repo Roots" disabled={!config} onClick={() => setRepoRootsOpen(true)}>⚙</button></div></div>
          {repoChoice === "__custom__" && <input value={customRepo} onChange={(event) => setCustomRepo(event.target.value)} placeholder="/path/to/repo" aria-label="Repo 路徑" />}
          {repoStatus && <div className={`repo-status${repoStatus.error || !repoStatus.tree_clean ? " warning" : ""}`}>
            {repoStatus.error ? `❌ ${repoStatus.error}` : <>goal.md {repoMark(repoStatus.goal)} · 工作樹 {repoStatus.tree_clean ? "✅ 乾淨" : "❌ 髒（preflight 會擋）"}{matchingWorkspace && ` · workspace「${matchingWorkspace.name}」已存在`}</>}
          </div>}
          <label>goal.md <span className="label-help">留空＝沿用 repo 已 commit 的版本</span>
            <input type="file" accept=".md,.markdown,.txt" onChange={(event) => setGoalFile(event.target.files?.[0] ?? null)} />
          </label>
          <PlanImportField value={planJson} onChange={setPlanJson} startPhase={startPhase} onStartPhaseChange={setStartPhase} />
          <label>Workspace 名稱 <span className="label-help">留空＝repo 目錄名</span><input value={name} onChange={(event) => setName(event.target.value)} /></label>
          <div className="form-columns command-columns">
            <div className="form-field agent-command-field"><span className="field-label-row"><span>Agent 命令</span></span><div className="command-select-row"><select aria-label="Agent 命令" value={agentIndex} onChange={(event) => setAgentIndex(event.target.value)}>{(config?.agent_cmds ?? []).map((agent, index) => <option key={agent.cmd} value={index}>{agent.label} — {agent.cmd}</option>)}</select><button type="button" className="icon-button cli-gear-button" aria-label="管理 Agent CLI" disabled={!config || !repo || !!repoStatus?.error} onClick={() => setCliManagerOpen(true)}>⚙</button></div></div>
            <div className="form-field validate-command-field"><span className="field-label-row"><span>Validate 命令</span><button type="button" className="secondary-button compact-button" disabled={validating || !repo || !!repoStatus?.error || (validateChoice === "__custom__" && !customValidate.trim())} onClick={() => void verifyValidate()}>{validating ? "執行中…" : "執行確認"}</button></span><select aria-label="Validate 命令" value={validateChoice} onChange={(event) => { setValidateChoice(event.target.value); setValidateResult(null); }}>{(config?.validate_cmds ?? []).map((command, index) => <option key={command.cmd} value={index}>{command.label} — {command.cmd}</option>)}<option value="__custom__">手寫…</option></select></div>
          </div>
          {validateChoice === "__custom__" && <input value={customValidate} onChange={(event) => { setCustomValidate(event.target.value); setValidateResult(null); }} placeholder="mvn -q test" aria-label="自訂 Validate 命令" />}
          {validateResult && <div className={`validate-result${validateResult.ok ? " success" : " error"}`} role="status"><strong>{validateResult.text}</strong>{validateResult.tail && <pre>{validateResult.tail}</pre>}</div>}
          <details className="advanced-settings">
            <summary>進階設定</summary>
            <div className="number-grid">
              <label>flag 收斂（&gt;）<input type="number" min={1} value={flagThreshold} onChange={(event) => setFlagThreshold(+event.target.value)} /></label>
              <label>done 收斂（≥）<input type="number" min={1} value={doneThreshold} onChange={(event) => setDoneThreshold(+event.target.value)} /></label>
              <label>單輪上限（分）<input type="number" min={0} value={roundTimeout} onChange={(event) => setRoundTimeout(+event.target.value)} /></label>
              <label>Agent 異常退避上限（秒）<input type="number" min={0} value={agentBackoffMax} onChange={(event) => setAgentBackoffMax(+event.target.value)} /></label>
              <label>Validate 上限（秒）<input type="number" min={1} value={validateTimeout} onChange={(event) => setValidateTimeout(+event.target.value)} /></label>
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
      {cliManagerOpen && config && <CliManagerModal config={config} repo={repo} onClose={() => setCliManagerOpen(false)} onSaved={(next) => {
        const selectedCommand = config.agent_cmds[+agentIndex]?.cmd;
        const nextIndex = next.agent_cmds.findIndex((agent) => agent.cmd === selectedCommand);
        setConfig(next);
        setAgentIndex(String(nextIndex >= 0 ? nextIndex : 0));
      }} />}
      {repoRootsOpen && config && <RepoRootsModal config={config} onClose={() => setRepoRootsOpen(false)} onSaved={(next) => {
        setConfig(next);
        if (repo !== "" && next.repos.includes(repo)) return;
        setRepoChoice(next.repos[0] ?? "__custom__");
      }} />}
    </Modal>
  );
}
