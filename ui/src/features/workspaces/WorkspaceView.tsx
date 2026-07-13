/** 單一 workspace 操作面：把 state 投影成計畫、健康、console 與安全操作，所有 mutation 都走後端 API。 */
import { useState } from "react";
import { postJson, waitForJobStartup } from "../../shared/api/client";
import ActionDialog, { type ActionPreviewItem } from "../../shared/components/ActionDialog";
import type { DashboardConfig, PlanEditTask, StartupResponse, WorkspaceState, WorkspaceSummary } from "../../shared/api/types";
import ConsolePane from "../console/ConsolePane";
import HorizontalSplitter from "../layout/HorizontalSplitter";
import ConfigModal from "./ConfigModal";
import GoalModal from "./GoalModal";
import HistoryModal from "./HistoryModal";
import IssuesModal from "./IssuesModal";
import PlanTable from "./PlanTable";
import PromptModal from "./PromptModal";
import ReportModal from "./ReportModal";
import RoundSparkline from "./RoundSparkline";
import RunCompareModal from "./RunCompareModal";
import TimelineModal from "./TimelineModal";
import { deriveRoundTiming, useRoundNow } from "./roundTiming";
import useStatusPulse from "./useStatusPulse";

const PHASE_NAMES = { plan: "規劃期", exec: "執行期", done: "🏁 完成" };
type WorkspaceModal = "config" | "goal" | "history" | "issues" | "report" | "prompt" | "timeline" | "runCompare";
const WORKSPACE_MUTATIONS = {
  run: { url: "/api/run", busy: "run" },
  drain: { url: "/api/drain", busy: "drain" },
  cancelDrain: { url: "/api/cancel-drain", busy: "cancelDrain" },
  stop: { url: "/api/stop", busy: "stop" },
  phase: { url: "/api/phase", busy: null },
  setTask: { url: "/api/set-task", busy: null },
} as const;
type WorkspaceMutation = keyof typeof WORKSPACE_MUTATIONS;

export default function WorkspaceView({
  workspace,
  state,
  consoleText,
  readonly,
  onRefresh,
  onRefreshWorkspaces,
  onLaunchFromTemplate
}: {
  workspace?: WorkspaceSummary;
  state: WorkspaceState | null;
  consoleText: string;
  readonly: boolean;
  onRefresh: () => void;
  onRefreshWorkspaces: () => void | Promise<void>;
  /** 以目前 workspace 的 config 為範本開啟啟動表單；執行中／停止／完成都可當範本。 */
  onLaunchFromTemplate: (config: DashboardConfig) => void;
}) {
  // 這些 Modal 在操作流程上互斥；用 union 讓「同時開兩個」成為不可表示的狀態。
  const [activeModal, setActiveModal] = useState<WorkspaceModal | null>(null);
  const [statusHeight, setStatusHeight] = useState(() => +(localStorage.getItem("status-console-height") || 220));
  const [statusCollapsed, setStatusCollapsed] = useState(() => localStorage.getItem("status-console-collapsed") === "1");
  const [busyAction, setBusyAction] = useState<"run" | "drain" | "cancelDrain" | "stop" | null>(null);
  const [dialog, setDialog] = useState<{
    title: string;
    message: string;
    confirmLabel?: string;
    danger?: boolean;
    preview?: ActionPreviewItem[];
    onConfirm?: () => void;
  } | null>(null);
  const canChange = !!workspace && !readonly && !workspace.running;
  const pulse = useStatusPulse(state);
  const roundNow = useRoundNow(Boolean(state?.round_started_at && !state.round_interrupted_at));
  const resizeStatus = (pixels: number) => {
    setStatusHeight(pixels);
    localStorage.setItem("status-console-height", String(pixels));
  };
  const toggleStatus = () => {
    setStatusCollapsed((value) => {
      localStorage.setItem("status-console-collapsed", value ? "0" : "1");
      return !value;
    });
  };

  if (!state) return <section className="workspace-pane"><div className="loading-state">載入 workspace…</div></section>;
  if (state.error) return <section className="workspace-pane"><div className="loading-state error">{state.error === "busy" ? "state 更新中…" : state.error}</div></section>;

  const mutate = async (action: WorkspaceMutation, body: unknown) => {
    // run/stop/drain 共用 startup/error 處理；若是啟動操作，不能只看 POST 成功，還要等 ready marker。
    const mutation = WORKSPACE_MUTATIONS[action];
    setBusyAction(mutation.busy);
    try {
      const response = await postJson<StartupResponse>(mutation.url, body);
      if (response.error) {
        setDialog({ title: "操作失敗", message: response.error });
        return;
      }
      if (response.starting && response.name && response.pid) {
        const startup = await waitForJobStartup(response.name, response.pid, response.startup_timeout);
        if (startup.error) {
          setDialog({ title: "啟動失敗", message: startup.error });
          await Promise.all([Promise.resolve(onRefresh()), Promise.resolve(onRefreshWorkspaces())]);
          return;
        }
      }
      await Promise.all([Promise.resolve(onRefresh()), Promise.resolve(onRefreshWorkspaces())]);
    } finally {
      setBusyAction(null);
    }
  };
  const changePhase = (phase: "plan" | "exec") => {
    // phase 變更會重設不同範圍的 coordinator 計數，先用結構化 preview 明列清除與保留內容。
    const message = phase === "exec"
      ? "直接進入執行期，從第一個任務開始。繼續？"
      : "回到規劃期會清除執行進度與完成紀錄，計畫保留。繼續？";
    setDialog({
      title: "請確認",
      message,
      confirmLabel: "繼續",
      preview: phase === "plan" ? [
        { label: "清除進度", value: `${completed} 筆完成紀錄、done ${state.done_count}`, tone: completed || state.done_count ? "warning" : undefined },
        { label: "重設暫態", value: `flag ${state.flag}、紅連跳 ${state.red_streak}、停滯 ${state.stall_rounds} → 0` },
        { label: "保留", value: `plan v${state.plan_version}（${total} 項）與 target repo 程式碼`, tone: "safe" },
      ] : [
        { label: "開始任務", value: state.plan?.[0] ? `task-${state.plan[0].order}：${state.plan[0].task}` : "無計畫" },
        { label: "重設暫態", value: `flag、done、紅連跳、停滯 → 0` },
        { label: "保留", value: `plan v${state.plan_version} 與 target repo 程式碼`, tone: "safe" },
      ],
      onConfirm: () => {
        setDialog(null);
        void mutate("phase", { name: workspace?.name, phase });
      }
    });
  };
  const gotoTask = (order: number) => {
    // 往後跳代表略過尚未完成任務，後端必須先 Validate 並標成人工完成；
    // 往前退則清除目標含以後的完成紀錄，但絕不改動 target repo。
    const done = new Set((state.completed ?? []).map((entry) => entry.order));
    const skipped = (state.plan ?? []).map((task) => task.order).filter((value) => value < order && !done.has(value));
    const message = skipped.length
      ? `跳到 task-${order}：task ${skipped.join(", ")} 會標記為人工確認完成，並先跑 validate。繼續？`
      : `退回 task-${order}：這個任務以後的完成紀錄會清除，code 不會動。繼續？`;
    const removed = (state.completed ?? []).filter((entry) => entry.order >= order).map((entry) => `task-${entry.order}`);
    setDialog({
      title: "請確認",
      message,
      confirmLabel: "繼續",
      preview: [
        ...(skipped.length ? [{ label: "人工標記完成", value: skipped.map((value) => `task-${value}`).join(", "), tone: "warning" as const }] : []),
        ...(skipped.length ? [{ label: "執行 Validate", value: `${state.config?.validate_cmd ?? "未設定"}（timeout ${state.config?.validate_timeout ?? 120} 秒）` }] : []),
        { label: "清除完成紀錄", value: removed.length ? removed.join(", ") : "無", tone: removed.length ? "warning" : undefined },
        { label: "保留", value: "target repo、commit 與工作樹內容完全不變", tone: "safe" },
      ],
      onConfirm: () => {
        setDialog(null);
        void mutate("setTask", { name: workspace?.name, order });
      }
    });
  };
  const savePlan = async (tasks: PlanEditTask[], doneCount: number) => {
    const response = await postJson<{ changed?: string[] }>("/api/edit-state", { name: workspace?.name, tasks, done_count: doneCount, plan_edit: true, plan_version: state.plan_version });
    if (response.error) return `❌ ${response.error}`;
    onRefresh();
    return `✅ 已儲存 ${response.changed?.join(", ") || "（無變更）"}`;
  };
  const deleteWorkspace = () => {
    setDialog({
      title: "確認刪除 workspace",
      message: `永久刪除 ${workspace?.name}？整個 workspace 的資料會直接移除，無法復原；target repo 與程式碼不受影響。`,
      confirmLabel: "永久刪除",
      danger: true,
      preview: [
        { label: "永久刪除", value: `workspace/${workspace?.name}`, tone: "warning" },
        { label: "包含", value: "state、history、console、logs、prompts、snapshots 與 REPORT" },
        { label: "目前狀態", value: `${state.phase} · round ${state.round} · 任務 ${completed}/${total}` },
        { label: "不受影響", value: state.config?.repo ? `target repo：${state.config.repo}` : "target repo 與程式碼", tone: "safe" },
      ],
      onConfirm: () => {
        setDialog(null);
        void (async () => {
          const response = await postJson<{ ok?: boolean; deleted?: boolean }>("/api/delete-workspace", { name: workspace?.name });
          if (response.error) {
            setDialog({ title: "操作失敗", message: response.error });
            return;
          }
          await Promise.resolve(onRefreshWorkspaces());
        })();
      }
    });
  };

  const completed = (state.completed ?? []).length;
  const total = (state.plan ?? []).length;
  const issues = state.issues ?? [];
  const issuesAcknowledgedRound = state.issues_acknowledged_round ?? -1;
  const unreadIssues = issues.filter((issue) => !Number.isInteger(issue.round) || issue.round > issuesAcknowledgedRound).length;
  const redLimit = state.config?.red_limit ?? 20;
  const stallLimit = state.config?.stall_limit ?? 300;
  const healthIntensity = state.phase === "done" ? 0 : Math.min(1, Math.max((state.red_streak ?? 0) / redLimit, (state.stall_rounds ?? 0) / stallLimit));
  const healthHue = Math.round(120 * (1 - healthIntensity));
  const healthLabel = state.phase === "done" ? "健康度：工作區已完成" : `健康度：紅連跳 ${state.red_streak}/${redLimit} · 停滯 ${state.stall_rounds}/${stallLimit}（越紅越接近 reset 防線）`;
  const roundTiming = deriveRoundTiming(state, workspace?.running ?? false, roundNow);
  return (
    <section className="workspace-pane">
      <header className="workspace-header">
        <div className="health-strip" role="img" aria-label={healthLabel} title={healthLabel}>
          <div className="health-strip-fill" style={{ background: `hsl(${healthHue} 72% 42%)`, opacity: 0.3 + healthIntensity * 0.7 }} />
        </div>
        <div className="workspace-title-row">
          <div className="workspace-title"><h1>{workspace?.name ?? "workspace"}</h1><span key={state.phase} className={`phase-badge phase-${state.phase}${pulse.has("phase") ? " status-pulse" : ""}`}>{PHASE_NAMES[state.phase]}</span></div>
          {!readonly && workspace && <div className="workspace-actions">
            {workspace.running && (workspace.draining
              ? workspace.drain_claimed
                ? <button type="button" className="secondary-button" disabled title="loop 已接手停止請求，會在本輪完整收尾後停止">⏳ 本輪收尾中</button>
                : <button type="button" className="secondary-button" disabled={busyAction !== null} onClick={() => void mutate("cancelDrain", { name: workspace.name })}>{busyAction === "cancelDrain" ? "撤銷中…" : "↩ 繼續運行"}</button>
              : <button type="button" className="secondary-button" disabled={busyAction !== null} onClick={() => void mutate("drain", { name: workspace.name })}>{busyAction === "drain" ? "要求中…" : "⏸ 本輪後停止"}</button>)}
            <button type="button" className={workspace.running ? "danger-button" : "success-button"} disabled={busyAction !== null} onClick={() => void mutate(workspace.running ? "stop" : "run", { name: workspace.name })}>{busyAction === "stop" ? "停止中…" : busyAction === "run" ? "啟動中…" : workspace.running ? "⏹ 立即停止" : "▶ 運行"}</button>
            {canChange && state.phase === "plan" && total > 0 && <button type="button" className="secondary-button" onClick={() => changePhase("exec")}>⏩ 進執行期</button>}
            {canChange && (state.phase === "exec" || state.phase === "done") && <button type="button" className="secondary-button" onClick={() => changePhase("plan")}>⏪ 回規劃期</button>}
            {canChange && <button type="button" className="secondary-button" onClick={() => setActiveModal("config")}>⚙ 設定</button>}
            {canChange && <button type="button" className="danger-button" onClick={deleteWorkspace}>🗑 刪除</button>}
            <button type="button" className="secondary-button" disabled={!state.config} title={state.config ? "以這個 workspace 的設定預填啟動表單" : "state 缺少 config 區塊，無法以此為範本"} onClick={() => state.config && onLaunchFromTemplate(state.config)}>📋 以此為範本啟動</button>
          </div>}
        </div>
        <div className="workspace-status-row">
          <div className="primary-status">
            <button type="button" className="chip subdued" onClick={() => setActiveModal("goal")}>🎯 goal</button>
            <span className="chip">round {state.round}</span>
            {state.phase !== "plan" && total > 0 && <span key={`${completed}-${state.current_order}`} className={`chip${pulse.has("task") ? " status-pulse" : ""}`}>任務 {completed}/{total}</span>}
            {state.phase === "plan" && <span key={state.flag} className={`chip${pulse.has("flag") ? " status-pulse" : ""}`}>flag {state.flag} / &gt;{state.config?.flag_threshold ?? 10}</span>}
            {state.phase === "plan" && state.config?.pause_after_plan && <span className="chip subdued" title="規劃收斂後 loop 會停止，需按「▶ 運行」開始執行期">⏸ 規劃後暫停</span>}
            {state.phase === "exec" && <span key={state.done_count} className={`chip${pulse.has("done") ? " status-pulse" : ""}`}>done {state.done_count} / ≥{state.config?.done_threshold ?? 3}</span>}
            {state.phase === "done" && <button type="button" className="chip report-chip" onClick={() => setActiveModal("report")}>📄 完成報告</button>}
          </div>
          <div className="health-status">
            {state.round > 0 && workspace && <RoundSparkline workspace={workspace.name} round={state.round} onOpen={() => setActiveModal("history")} />}
            {state.phase !== "done" && <span key={`${state.red_streak}-${state.stall_rounds}`} className={`chip subdued${state.phase === "plan" && state.plan_version >= 10 ? " warning" : ""}${pulse.has("health") ? " status-pulse" : ""}`}>紅連跳 {state.red_streak} · 停滯 {state.stall_rounds} · plan v{state.plan_version}{state.phase === "plan" && state.plan_version >= 10 ? " ⚠ 可能震盪" : ""}</span>}
            {!!state.agent_failure_streak && <span key={`${state.agent_failure_streak}-${state.agent_backoff_seconds}`} className={`chip warning${pulse.has("health") ? " status-pulse" : ""}`}>Agent 異常 {state.agent_failure_streak}{state.agent_backoff_seconds ? ` · ${state.agent_backoff_seconds} 秒後重試` : ""}</span>}
            {roundTiming && <span data-testid="round-timer" className={`chip round-timer ${roundTiming.warning || roundTiming.interrupted ? "warning" : "subdued"}`} title={`開始 ${state.round_started_at}${state.round_deadline_at ? ` · deadline ${state.round_deadline_at}` : ""}`}>{roundTiming.label}</span>}
            {(state.last_round_seconds ?? 0) > 0 && <span className={`chip ${state.last_round_timed_out ? "warning" : "subdued"}`}>⏱ 上輪 {state.last_round_seconds} 秒{state.last_round_timed_out ? " · 逾時" : ""}</span>}
            {!!state.state_recovery_count && <span className="chip warning" title={state.last_state_recovery ?? undefined}>🛟 state 復原 {state.state_recovery_count}</span>}
            {state.state_recovery_pending && <span className="chip warning">🛟 正從 checkpoint 唯讀顯示</span>}
            {workspace?.stale_loop_pid && <span className="chip warning" title={`state 保留 PID ${workspace.loop_pid ?? "?"}${workspace.loop_started_at ? `（啟動於 ${workspace.loop_started_at}）` : ""}，但目前程序不存在`}>⚠ PID 殘留</span>}
            {!!issues.length && <button type="button" className={`chip ${unreadIssues > 0 ? "issue-chip" : "subdued"}`} onClick={() => setActiveModal("issues")}>{unreadIssues > 0 ? `⚠ issues ${unreadIssues}/${issues.length}` : `✓ issues ${issues.length}（已讀）`}</button>}
            {state.round > 0 && <button type="button" className="chip subdued" onClick={() => setActiveModal("history")}>🕒 輪次紀錄</button>}
            {state.round > 0 && <button type="button" className="chip subdued" onClick={() => setActiveModal("timeline")}>🧭 時間軸</button>}
            {state.round > 0 && <button type="button" className="chip subdued" onClick={() => setActiveModal("runCompare")}>⇄ Run 對比</button>}
            {state.round > 0 && <button type="button" className="chip subdued" onClick={() => setActiveModal("prompt")}>📨 prompt</button>}
          </div>
        </div>
        {state.goal_changed && <div className="goal-warning">⚠ goal 已變更；點「🎯 goal」查看差異，建議回規劃期重新收斂</div>}
      </header>
      <div className="workspace-main">
        <PlanTable state={state} canEdit={canChange} onSave={savePlan} onGoto={gotoTask} />
        {!statusCollapsed && <HorizontalSplitter onResize={resizeStatus} />}
        <div className={`status-console-wrap${statusCollapsed ? " collapsed" : ""}`} style={{ height: statusCollapsed ? 40 : statusHeight }}>
          <ConsolePane
            text={consoleText}
            round={state.round}
            running={workspace?.running ?? false}
            hasWorkspace={!!workspace}
            title="Loop 狀態紀錄"
            ariaLabel="Loop 狀態紀錄"
            defaultFilter="other"
            showFilters={false}
            collapsed={statusCollapsed}
            onToggleCollapse={toggleStatus}
          />
        </div>
      </div>
      {activeModal === "issues" && workspace && <IssuesModal workspace={workspace.name} issues={issues} unreadIssues={unreadIssues} readonly={readonly || workspace.running} onClose={() => setActiveModal(null)} onChanged={onRefresh} />}
      {activeModal === "history" && workspace && <HistoryModal workspace={workspace.name} onClose={() => setActiveModal(null)} />}
      {activeModal === "timeline" && workspace && <TimelineModal workspace={workspace.name} consoleText={consoleText} onClose={() => setActiveModal(null)} />}
      {activeModal === "runCompare" && workspace && <RunCompareModal workspace={workspace.name} onClose={() => setActiveModal(null)} />}
      {activeModal === "goal" && workspace && <GoalModal workspace={workspace.name} onClose={() => setActiveModal(null)} />}
      {activeModal === "prompt" && workspace && <PromptModal workspace={workspace.name} onClose={() => setActiveModal(null)} />}
      {activeModal === "report" && workspace && <ReportModal workspace={workspace.name} onClose={() => setActiveModal(null)} />}
      {activeModal === "config" && workspace && <ConfigModal workspace={workspace.name} config={state.config ?? {}} plan={state.plan ?? []} onClose={() => setActiveModal(null)} onChanged={onRefresh} />}
      {dialog && <ActionDialog title={dialog.title} message={dialog.message} confirmLabel={dialog.confirmLabel} danger={dialog.danger} preview={dialog.preview} onClose={() => setDialog(null)} onConfirm={dialog.onConfirm} />}
    </section>
  );
}
