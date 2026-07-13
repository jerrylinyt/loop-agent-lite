/** Loop 啟動中心：彙整 repo/goal/plan/CLI/Validate 設定，先預覽差異與 preflight，再送交易式 launch。 */
import { useEffect, useMemo, useRef, useState } from "react";
import CliManagerModal from "../cli/CliManagerModal";
import GoalTemplateModal from "./GoalTemplateModal";
import Modal from "../../shared/components/Modal";
import { getJson, postJson, waitForJobStartup } from "../../shared/api/client";
import useStaleGuard from "../../shared/hooks/useStaleGuard";
import type { ConfigResponse, DashboardConfig, StartupResponse, WorkspaceState, WorkspaceSummary } from "../../shared/api/types";
import PlanImportField from "./PlanImportField";
import NotifyModal from "./NotifyModal";
import PromptTemplateModal from "./PromptTemplateModal";
import RepoRootsModal from "./RepoRootsModal";
import { validatePlan } from "./planValidation";
import { isPromptTemplateBundleSupported, type PromptTemplateMode } from "./promptTemplateBuilder";
import LauncherJobs from "./LauncherJobs";

interface RepoStatus { goal: "committed" | "modified" | "untracked" | "missing"; tree_clean: boolean; branch?: string; suggested_validate_cmd?: string | null; error?: string }
interface ValidateResponse { ok?: boolean; rc?: number; timeout?: boolean; timeout_seconds?: number; tail?: string }
interface PreflightResponse extends ValidateResponse { error?: string }
interface ExecutionSettings {
  flagThreshold: number;
  doneThreshold: number;
  roundTimeout: number;
  agentBackoffMax: number;
  validateTimeout: number;
  pauseAfterPlan: boolean;
}
const DEFAULT_EXECUTION_SETTINGS: ExecutionSettings = {
  flagThreshold: 10,
  doneThreshold: 3,
  roundTimeout: 30,
  agentBackoffMax: 60,
  validateTimeout: 120,
  pauseAfterPlan: false,
};

function ExecutionSettingsFields({ value, onChange }: {
  value: ExecutionSettings;
  onChange: (patch: Partial<ExecutionSettings>) => void;
}) {
  return (
    <div className="number-grid">
      <label>flag 收斂（&gt;）<input type="number" min={1} value={value.flagThreshold} onChange={(event) => onChange({ flagThreshold: +event.target.value })} /></label>
      <label>done 收斂（≥）<input type="number" min={1} value={value.doneThreshold} onChange={(event) => onChange({ doneThreshold: +event.target.value })} /></label>
      <label>單輪上限（分）<input type="number" min={0} value={value.roundTimeout} onChange={(event) => onChange({ roundTimeout: +event.target.value })} /></label>
      <label>Agent 異常退避上限（秒）<input type="number" min={0} value={value.agentBackoffMax} onChange={(event) => onChange({ agentBackoffMax: +event.target.value })} /></label>
      <label>Validate 上限（秒）<input type="number" min={1} value={value.validateTimeout} onChange={(event) => onChange({ validateTimeout: +event.target.value })} /></label>
    </div>
  );
}

export default function LauncherModal({
  workspaces,
  templateConfig,
  onClose,
  onLaunched
}: {
  workspaces: WorkspaceSummary[];
  /** 「以此為範本啟動」帶入的來源 workspace config；只預填表單，不影響既有驗證與啟動路徑。 */
  templateConfig?: DashboardConfig | null;
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
  // 執行參數是一組共同載入/回填/送出的表單資料，集中更新避免 repo 切換時出現半套設定。
  const [settings, setSettings] = useState<ExecutionSettings>(DEFAULT_EXECUTION_SETTINGS);
  const [resetState, setResetState] = useState(false);
  const [newBranch, setNewBranch] = useState(false);
  const [message, setMessage] = useState("");
  const [launching, setLaunching] = useState(false);
  const [validating, setValidating] = useState(false);
  const [preflighting, setPreflighting] = useState(false);
  const [validateResult, setValidateResult] = useState<{ ok: boolean; text: string; tail: string } | null>(null);
  const [preflightResult, setPreflightResult] = useState<{ ok: boolean; text: string; tail: string } | null>(null);
  const [managerModal, setManagerModal] = useState<"cli" | "repoRoots" | "notify" | null>(null);
  const [promptTemplateMode, setPromptTemplateMode] = useState<PromptTemplateMode | null>(null);
  const [goalTemplateOpen, setGoalTemplateOpen] = useState(false);
  const hydratedRepo = useRef("");
  const appliedTemplate = useRef(false);
  const validateGuard = useStaleGuard();
  const preflightGuard = useStaleGuard();

  const repo = repoChoice === "__custom__" ? customRepo.trim() : repoChoice;
  const matchingWorkspace = useMemo(() => workspaces.find((workspace) => workspace.repo === repo), [repo, workspaces]);
  const promptTemplateAvailable = !!config?.prompt_templates?.length
    && isPromptTemplateBundleSupported(config.prompt_template_bundle);
  const promptTemplateUnavailableReason = config?.prompt_templates?.length && !promptTemplateAvailable
    ? config.prompt_template_bundle_error || "固定 Prompt 資源未載入或版本不相容，請重新啟動 Dashboard 後再試。"
    : "";

  useEffect(() => {
    void getJson<ConfigResponse>("/api/config").then((response) => {
      setConfig(response);
      if (!response) return setMessage("❌ 設定載入失敗");
      if (response.error) return setMessage(`❌ ${response.error}`);
      // 範本模式 fail-closed：repo 只由範本 hydration 決定；範本缺 repo 就留空，不落回預設 repo。
      if (!templateConfig) setRepoChoice(response.repos[0] ?? "__custom__");
      setSettings({
        flagThreshold: response.defaults.flag_threshold ?? 10,
        doneThreshold: response.defaults.done_threshold ?? 3,
        roundTimeout: response.defaults.round_timeout ?? 30,
        agentBackoffMax: response.defaults.agent_backoff_max ?? 60,
        validateTimeout: response.defaults.validate_timeout ?? 120,
        pauseAfterPlan: response.defaults.pause_after_plan ?? false,
      });
    });
  }, [templateConfig]);

  useEffect(() => {
    // 以範本 workspace 的 config 預填欄位；只跑一次，workspace 名稱刻意留給使用者填新的。
    if (!config || !templateConfig || appliedTemplate.current) return;
    appliedTemplate.current = true;
    const repoValue = (templateConfig.repo ?? "").trim();
    if (repoValue) {
      if (config.repos.includes(repoValue)) {
        setRepoChoice(repoValue);
        setCustomRepo("");
      } else {
        setRepoChoice("__custom__");
        setCustomRepo(repoValue);
      }
    } else {
      // fail-closed：範本缺 repo 就讓欄位留空給使用者自己填，不落回 /api/config 的預設 repo。
      setRepoChoice("__custom__");
      setCustomRepo("");
    }
    if (templateConfig.agent_cmd) {
      const agentMatch = config.agent_cmds.findIndex((item) => item.cmd === templateConfig.agent_cmd);
      if (agentMatch >= 0) setAgentIndex(String(agentMatch));
    }
    if (templateConfig.validate_cmd) {
      const validateMatch = config.validate_cmds.findIndex((item) => item.cmd === templateConfig.validate_cmd);
      if (validateMatch >= 0) {
        setValidateChoice(String(validateMatch));
        setCustomValidate("");
      } else {
        setValidateChoice("__custom__");
        setCustomValidate(templateConfig.validate_cmd);
      }
    }
    setSettings((value) => ({
      flagThreshold: templateConfig.flag_threshold ?? value.flagThreshold,
      doneThreshold: templateConfig.done_threshold ?? value.doneThreshold,
      roundTimeout: templateConfig.round_timeout ?? value.roundTimeout,
      agentBackoffMax: templateConfig.agent_backoff_max ?? value.agentBackoffMax,
      validateTimeout: templateConfig.validate_timeout ?? value.validateTimeout,
      pauseAfterPlan: templateConfig.pause_after_plan ?? value.pauseAfterPlan,
    }));
  }, [config, templateConfig]);

  useEffect(() => {
    if (!repo) return setRepoStatus(null);
    let active = true;
    void getJson<RepoStatus>(`/api/repo-status?repo=${encodeURIComponent(repo)}`).then((status) => {
      if (active) setRepoStatus(status);
    });
    return () => { active = false; };
  }, [repo]);

  useEffect(() => {
    // 範本模式無條件停用自動回填：欄位一律以範本 config 為準（含使用者後續手動改動），
    // 不讓同 repo 既有 workspace 的保存設定或 Validate 建議覆寫範本值。
    if (templateConfig) return;
    if (!config || !repo || !repoStatus || hydratedRepo.current === repo) return;
    // 同 repo 已有 workspace 時以保存設定回填；否則只套用 repo 偵測出的 Validate 建議。
    // hydratedRepo 防止 SSE/狀態重繪覆蓋使用者正在修改的表單。
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
          setSettings({
            flagThreshold: saved.flag_threshold ?? config.defaults.flag_threshold ?? 10,
            doneThreshold: saved.done_threshold ?? config.defaults.done_threshold ?? 3,
            roundTimeout: saved.round_timeout ?? config.defaults.round_timeout ?? 30,
            agentBackoffMax: saved.agent_backoff_max ?? config.defaults.agent_backoff_max ?? 60,
            validateTimeout: saved.validate_timeout ?? config.defaults.validate_timeout ?? 120,
            pauseAfterPlan: saved.pause_after_plan ?? config.defaults.pause_after_plan ?? false,
          });
          return;
        }
      }
      selectValidate(repoStatus.suggested_validate_cmd);
    })();
    return () => { active = false; };
  }, [config, matchingWorkspace, repo, repoStatus, templateConfig]);

  useEffect(() => {
    validateGuard.cancelPending();
    setValidating(false);
    setValidateResult(null);
  }, [customValidate, repo, settings.validateTimeout, validateChoice, validateGuard]);

  useEffect(() => {
    preflightGuard.cancelPending();
    setPreflighting(false);
    setPreflightResult(null);
  }, [goalFile, name, newBranch, planJson, preflightGuard, repo, resetState, settings.validateTimeout, validateChoice]);

  const launch = async () => {
    // 前端先擋明顯格式錯誤以提供即時回饋；後端仍會重新校驗 plan、路徑、Git 與數值。
    const planError = validatePlan(planJson);
    if (planError) return setMessage(`❌ plan.json 格式不對：${planError}`);
    setLaunching(true);
    setMessage("啟動中…");
    const body: Record<string, unknown> = {
      repo,
      name: name.trim(),
      agent_idx: +agentIndex,
      flag_threshold: settings.flagThreshold,
      done_threshold: settings.doneThreshold,
      round_timeout: settings.roundTimeout,
      agent_backoff_max: settings.agentBackoffMax,
      validate_timeout: settings.validateTimeout,
      pause_after_plan: settings.pauseAfterPlan,
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
      // 收到 pid 只代表 process 已 spawn；必須等待 startup handshake 才能關閉表單。
      setMessage("啟動前檢查中…");
      const startup = await waitForJobStartup(response.name, response.pid, response.startup_timeout ?? settings.validateTimeout + 15);
      if (startup.error) {
        setLaunching(false);
        return setMessage(`❌ ${startup.error}`);
      }
    }
    setLaunching(false);
    setMessage(`✅ 已啟動 ${response.name}（pid ${response.pid}）`);
    onLaunched(response.name);
  };

  const verifyValidate = async () => {
    const isCurrent = validateGuard.begin();
    const validateCmd = validateChoice === "__custom__"
      ? customValidate.trim()
      : config?.validate_cmds[+validateChoice]?.cmd ?? "";
    setValidating(true);
    setValidateResult(null);
    const response = await postJson<ValidateResponse>("/api/validate", { repo, validate_cmd: validateCmd, validate_timeout: settings.validateTimeout });
    if (!isCurrent()) return;
    setValidating(false);
    if (response.error) {
      setValidateResult({ ok: false, text: `❌ ${response.error}`, tail: "" });
      return;
    }
    setValidateResult({
      ok: !!response.ok,
      text: response.timeout ? `❌ 執行逾時（${response.timeout_seconds ?? settings.validateTimeout} 秒）` : response.ok ? "✅ Validate 通過（exit 0）" : `❌ Validate 失敗（exit ${response.rc ?? "?"}）`,
      tail: response.tail ?? ""
    });
  };

  const hasPendingLaunchMutation = !!goalFile || !!planJson.trim() || resetState || newBranch;
  const preflightBlockedByDraft = hasPendingLaunchMutation || validateChoice === "__custom__";
  const canPreflight = !preflighting && !!repo && !repoStatus?.error && !hasPendingLaunchMutation && validateChoice !== "__custom__";
  const preflightHint = hasPendingLaunchMutation
    ? "完整健檢只檢查目前已 commit 的 repo；先清除待匯入的 goal、plan、重置或新 branch 選項"
    : validateChoice === "__custom__"
      ? "完整健檢只使用已儲存的 Validate 命令；請先把手寫命令加入清單"
      : "檢查目前 repo 的 git、單 writer lock、乾淨工作樹、已 commit 的 goal 與 Validate；不建立 state 或啟動 Agent";
  const runPreflight = async () => {
    const isCurrent = preflightGuard.begin();
    setPreflighting(true);
    setPreflightResult(null);
    const response = await postJson<PreflightResponse>("/api/preflight", {
      repo,
      name: name.trim(),
      validate_idx: +validateChoice,
      validate_timeout: settings.validateTimeout
    });
    if (!isCurrent()) return;
    setPreflighting(false);
    if (response.error) {
      setPreflightResult({ ok: false, text: `❌ ${response.error}`, tail: "" });
      return;
    }
    setPreflightResult({
      ok: !!response.ok,
      text: response.timeout ? `❌ 完整健檢逾時（${response.timeout_seconds ?? settings.validateTimeout + 15} 秒）` : response.ok ? "✅ 完整啟動前健檢通過" : `❌ 完整健檢未通過（exit ${response.rc ?? "?"}）`,
      tail: response.tail ?? ""
    });
  };

  const repoMark = (value?: string) => value === "committed" ? "✅ 已 commit" : value === "modified" ? "⚠ 已修改未 commit" : value === "untracked" ? "⚠ 尚未 commit" : "❌ 缺少";
  const selectedAgent = config?.agent_cmds[+agentIndex]?.cmd ?? "";
  const selectedValidate = validateChoice === "__custom__" ? customValidate.trim() : config?.validate_cmds[+validateChoice]?.cmd ?? "";
  const importedPlanCount = (() => { try { const value = JSON.parse(planJson); return Array.isArray(value) ? value.length : 0; } catch { return 0; } })();
  const launchChanges = [
    { label: "goal.md", before: repoStatus?.goal === "committed" ? "沿用已 commit 版本" : repoMark(repoStatus?.goal), after: goalFile ? `以 ${goalFile.name} 取代（${goalFile.size} bytes）` : "不變" },
    { label: "plan / phase", before: matchingWorkspace ? `${matchingWorkspace.plan_len ?? 0} tasks · ${matchingWorkspace.phase ?? "—"}` : "新 workspace", after: planJson.trim() ? `${importedPlanCount} tasks · ${startPhase}` : resetState ? "重建 state" : "沿用" },
    { label: "Agent", before: matchingWorkspace ? "已儲存設定" : "預設設定", after: selectedAgent || "—" },
    { label: "Validate", before: matchingWorkspace ? "已儲存設定" : "預設設定", after: `${selectedValidate || "—"} · ${settings.validateTimeout}s` },
    { label: "收斂 / timeout", before: matchingWorkspace ? "現有 workspace 設定" : "預設設定", after: `flag>${settings.flagThreshold} · done≥${settings.doneThreshold} · round ${settings.roundTimeout}m · backoff ${settings.agentBackoffMax}s${settings.pauseAfterPlan ? " · 規劃後暫停" : ""}` },
    { label: "Git branch", before: repoStatus?.branch || "detached / unknown", after: newBranch ? `建立 loop/${name.trim() || repo.split("/").filter(Boolean).slice(-1)[0] || "workspace"}` : "不切換" },
  ];
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
              </select><button type="button" className="icon-button cli-gear-button" aria-label="管理 Code Repo Roots" disabled={!config} onClick={() => setManagerModal("repoRoots")}>⚙</button></div></div>
          {repoChoice === "__custom__" && <input value={customRepo} onChange={(event) => setCustomRepo(event.target.value)} placeholder="/path/to/repo" aria-label="Repo 路徑" />}
          {repoStatus && <div className={`repo-status${repoStatus.error || !repoStatus.tree_clean ? " warning" : ""}`}>
            {repoStatus.error ? `❌ ${repoStatus.error}` : <>goal.md {repoMark(repoStatus.goal)} · 工作樹 {repoStatus.tree_clean ? "✅ 乾淨" : "❌ 髒（preflight 會擋）"}{matchingWorkspace && ` · workspace「${matchingWorkspace.name}」已存在`}</>}
          </div>}
          <div className="form-field">
            <div className="field-label-row"><label htmlFor="goal-file">goal.md <span className="label-help">留空＝沿用 repo 已 commit 的版本</span></label><span className="field-actions"><button type="button" className="text-button" title={promptTemplateUnavailableReason || undefined} disabled={!promptTemplateAvailable} onClick={() => setPromptTemplateMode("goal")}>Goal 產生器 Prompt</button><button type="button" className="text-button" title={promptTemplateUnavailableReason || undefined} disabled={!promptTemplateAvailable} onClick={() => setGoalTemplateOpen(true)}>Goal 成果模板</button></span></div>
            <input id="goal-file" type="file" accept=".md,.markdown,.txt" onChange={(event) => setGoalFile(event.target.files?.[0] ?? null)} />
          </div>
          <PlanImportField value={planJson} onChange={setPlanJson} startPhase={startPhase} onStartPhaseChange={setStartPhase} onOpenPromptTemplate={() => setPromptTemplateMode("plan")} promptTemplateAvailable={promptTemplateAvailable} />
          {promptTemplateUnavailableReason && <p className="field-error" role="alert">Prompt 模板停用：{promptTemplateUnavailableReason}</p>}
          <label>Workspace 名稱 <span className="label-help">留空＝repo 目錄名</span><input value={name} onChange={(event) => setName(event.target.value)} /></label>
          <div className="form-columns command-columns">
            <div className="form-field agent-command-field"><span className="field-label-row"><span>Agent 命令</span></span><div className="command-select-row"><select aria-label="Agent 命令" value={agentIndex} onChange={(event) => setAgentIndex(event.target.value)}>{(config?.agent_cmds ?? []).map((agent, index) => <option key={agent.cmd} value={index}>{agent.label} — {agent.cmd}</option>)}</select><button type="button" className="icon-button cli-gear-button" aria-label="管理 Agent CLI" disabled={!config || !repo || !!repoStatus?.error} onClick={() => setManagerModal("cli")}>⚙</button></div></div>
            <div className="form-field validate-command-field"><span className="field-label-row"><span>Validate 命令</span><span className="field-actions"><button type="button" className="secondary-button compact-button" disabled={validating || !repo || !!repoStatus?.error || (validateChoice === "__custom__" && !customValidate.trim())} onClick={() => void verifyValidate()}>{validating ? "執行中…" : "執行確認"}</button><button type="button" className="secondary-button compact-button" title={preflightHint} disabled={!canPreflight} onClick={() => void runPreflight()}>{preflighting ? "健檢中…" : "完整健檢"}</button></span></span><select aria-label="Validate 命令" value={validateChoice} onChange={(event) => { setValidateChoice(event.target.value); setValidateResult(null); }}>{(config?.validate_cmds ?? []).map((command, index) => <option key={command.cmd} value={index}>{command.label} — {command.cmd}</option>)}<option value="__custom__">手寫…</option></select></div>
          </div>
          {validateChoice === "__custom__" && <input value={customValidate} onChange={(event) => { setCustomValidate(event.target.value); setValidateResult(null); }} placeholder="mvn -q test" aria-label="自訂 Validate 命令" />}
          {preflightBlockedByDraft && <p className="field-help">ℹ {preflightHint}</p>}
          {validateResult && <div className={`validate-result${validateResult.ok ? " success" : " error"}`} role="status"><strong>{validateResult.text}</strong>{validateResult.tail && <pre>{validateResult.tail}</pre>}</div>}
          {preflightResult && <div className={`validate-result${preflightResult.ok ? " success" : " error"}`} role="status"><strong>{preflightResult.text}</strong>{preflightResult.tail && <pre>{preflightResult.tail}</pre>}</div>}
          <details className="advanced-settings">
            <summary>進階設定</summary>
            <ExecutionSettingsFields value={settings}
              onChange={(patch) => setSettings((value) => ({ ...value, ...patch }))} />
            <label className="checkbox-row"><input type="checkbox" checked={settings.pauseAfterPlan} onChange={(event) => setSettings((value) => ({ ...value, pauseAfterPlan: event.target.checked }))} />規劃收斂後暫停：不自動進入執行期，需回 Dashboard 按「▶ 運行」開始執行</label>
            <label className="checkbox-row"><input type="checkbox" checked={resetState} onChange={(event) => setResetState(event.target.checked)} />重置 workspace state（清除舊進度）</label>
            <label className="checkbox-row"><input type="checkbox" checked={newBranch} onChange={(event) => setNewBranch(event.target.checked)} />在新 branch 跑（loop/&lt;workspace 名&gt;）</label>
            <div className="notify-entry-row"><button type="button" className="secondary-button" disabled={!config} onClick={() => setManagerModal("notify")}>🔔 管理終態通知</button><span className="label-help">{config?.notify_cmd ? `目前：${config.notify_cmd}` : "目前未設定通知"}</span></div>
          </details>
          <details className="launch-diff" open={hasPendingLaunchMutation}>
            <summary>執行前變更 Diff <span className="label-help">送出前核對本次與既有狀態差異</span></summary>
            <div className="launch-diff-grid" role="list">
              {launchChanges.map((change) => <div className="launch-diff-row" role="listitem" key={change.label}><strong>{change.label}</strong><span className="diff-before">− {change.before}</span><span className="diff-after">＋ {change.after}</span></div>)}
            </div>
          </details>
        </div>
      ) : <LauncherJobs />}
      {managerModal === "cli" && config && <CliManagerModal config={config} repo={repo} onClose={() => setManagerModal(null)} onSaved={(next) => {
        const selectedCommand = config.agent_cmds[+agentIndex]?.cmd;
        const nextIndex = next.agent_cmds.findIndex((agent) => agent.cmd === selectedCommand);
        setConfig(next);
        setAgentIndex(String(nextIndex >= 0 ? nextIndex : 0));
      }} />}
      {managerModal === "notify" && config && <NotifyModal config={config} onClose={() => setManagerModal(null)} onSaved={setConfig} />}
      {managerModal === "repoRoots" && config && <RepoRootsModal config={config} onClose={() => setManagerModal(null)} onSaved={(next) => {
        setConfig(next);
        if (repo !== "" && next.repos.includes(repo)) return;
        setRepoChoice(next.repos[0] ?? "__custom__");
      }} />}
      {promptTemplateMode && config?.prompt_templates?.length && isPromptTemplateBundleSupported(config.prompt_template_bundle) && <PromptTemplateModal
        templates={config.prompt_templates}
        bundle={config.prompt_template_bundle}
        warnings={config.prompt_template_warnings}
        projectConfigPath={config.project_config_path}
        initialMode={promptTemplateMode}
        onClose={() => setPromptTemplateMode(null)}
      />}
      {goalTemplateOpen && isPromptTemplateBundleSupported(config?.prompt_template_bundle) && <GoalTemplateModal
        templates={config?.prompt_templates ?? []}
        bundle={config.prompt_template_bundle}
        warnings={config?.prompt_template_warnings}
        onClose={() => setGoalTemplateOpen(false)}
      />}
    </Modal>
  );
}
