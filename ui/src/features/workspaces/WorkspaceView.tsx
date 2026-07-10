import { useState } from "react";
import { postJson, waitForJobStartup } from "../../shared/api/client";
import ActionDialog from "../../shared/components/ActionDialog";
import type { PlanTask, StartupResponse, WorkspaceState, WorkspaceSummary } from "../../shared/api/types";
import ConsolePane from "../console/ConsolePane";
import HorizontalSplitter from "../layout/HorizontalSplitter";
import ConfigModal from "./ConfigModal";
import IssuesModal from "./IssuesModal";
import PlanTable from "./PlanTable";
import useStatusPulse from "./useStatusPulse";

const PHASE_NAMES = { plan: "規劃期", exec: "執行期", done: "🏁 完成" };

export default function WorkspaceView({
  workspace,
  state,
  consoleText,
  readonly,
  onRefresh,
  onRefreshWorkspaces
}: {
  workspace?: WorkspaceSummary;
  state: WorkspaceState | null;
  consoleText: string;
  readonly: boolean;
  onRefresh: () => void;
  onRefreshWorkspaces: () => void | Promise<void>;
}) {
  const [configOpen, setConfigOpen] = useState(false);
  const [issuesOpen, setIssuesOpen] = useState(false);
  const [statusHeight, setStatusHeight] = useState(() => +(localStorage.getItem("status-console-height") || 220));
  const [statusCollapsed, setStatusCollapsed] = useState(() => localStorage.getItem("status-console-collapsed") === "1");
  const [busyAction, setBusyAction] = useState<"run" | "stop" | null>(null);
  const [dialog, setDialog] = useState<{
    title: string;
    message: string;
    confirmLabel?: string;
    onConfirm?: () => void;
  } | null>(null);
  const canChange = !!workspace && !readonly && !workspace.running;
  const pulse = useStatusPulse(state);
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

  const mutate = async (url: string, body: unknown) => {
    setBusyAction(url === "/api/stop" ? "stop" : url === "/api/run" ? "run" : null);
    try {
      const response = await postJson<StartupResponse>(url, body);
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
    const message = phase === "exec"
      ? "直接進入執行期，從第一個任務開始。繼續？"
      : "回到規劃期會清除執行進度與完成紀錄，計畫保留。繼續？";
    setDialog({
      title: "請確認",
      message,
      confirmLabel: "繼續",
      onConfirm: () => {
        setDialog(null);
        void mutate("/api/phase", { name: workspace?.name, phase });
      }
    });
  };
  const gotoTask = (order: number) => {
    const done = new Set((state.completed ?? []).map((entry) => entry.order));
    const skipped = (state.plan ?? []).map((task) => task.order).filter((value) => value < order && !done.has(value));
    const message = skipped.length
      ? `跳到 task-${order}：task ${skipped.join(", ")} 會標記為人工確認完成，並先跑 validate。繼續？`
      : `退回 task-${order}：這個任務以後的完成紀錄會清除，code 不會動。繼續？`;
    setDialog({
      title: "請確認",
      message,
      confirmLabel: "繼續",
      onConfirm: () => {
        setDialog(null);
        void mutate("/api/set-task", { name: workspace?.name, order });
      }
    });
  };
  const savePlan = async (tasks: PlanTask[], doneCount: number) => {
    const response = await postJson<{ changed?: string[] }>("/api/edit-state", { name: workspace?.name, tasks, done_count: doneCount });
    if (response.error) return `❌ ${response.error}`;
    onRefresh();
    return `✅ 已儲存 ${response.changed?.join(", ") || "（無變更）"}`;
  };

  const completed = (state.completed ?? []).length;
  const total = (state.plan ?? []).length;
  return (
    <section className="workspace-pane">
      <header className="workspace-header">
        <div className="workspace-title-row">
          <div className="workspace-title"><h1>{workspace?.name ?? "workspace"}</h1><span key={state.phase} className={`phase-badge phase-${state.phase}${pulse.has("phase") ? " status-pulse" : ""}`}>{PHASE_NAMES[state.phase]}</span></div>
          {!readonly && workspace && <div className="workspace-actions">
            <button type="button" className={workspace.running ? "danger-button" : "success-button"} disabled={busyAction !== null} onClick={() => void mutate(workspace.running ? "/api/stop" : "/api/run", { name: workspace.name })}>{busyAction === "stop" ? "停止中…" : busyAction === "run" ? "啟動中…" : workspace.running ? "⏹ 停止" : "▶ 運行"}</button>
            {canChange && state.phase === "plan" && total > 0 && <button type="button" className="secondary-button" onClick={() => changePhase("exec")}>⏩ 進執行期</button>}
            {canChange && (state.phase === "exec" || state.phase === "done") && <button type="button" className="secondary-button" onClick={() => changePhase("plan")}>⏪ 回規劃期</button>}
            {canChange && <button type="button" className="secondary-button" onClick={() => setConfigOpen(true)}>⚙ 設定</button>}
          </div>}
        </div>
        <div className="workspace-status-row">
          <div className="primary-status">
            <span className="chip">round {state.round}</span>
            {state.phase !== "plan" && total > 0 && <span key={`${completed}-${state.current_order}`} className={`chip${pulse.has("task") ? " status-pulse" : ""}`}>任務 {completed}/{total}</span>}
            {state.phase === "plan" && <span key={state.flag} className={`chip${pulse.has("flag") ? " status-pulse" : ""}`}>flag {state.flag} / &gt;{state.config?.flag_threshold ?? 10}</span>}
            {state.phase === "exec" && <span key={state.done_count} className={`chip${pulse.has("done") ? " status-pulse" : ""}`}>done {state.done_count} / ≥{state.config?.done_threshold ?? 3}</span>}
          </div>
          <div className="health-status">
            <span key={`${state.red_streak}-${state.stall_rounds}`} className={`chip subdued${state.phase === "plan" && state.plan_version >= 10 ? " warning" : ""}${pulse.has("health") ? " status-pulse" : ""}`}>紅連跳 {state.red_streak} · 停滯 {state.stall_rounds} · plan v{state.plan_version}{state.phase === "plan" && state.plan_version >= 10 ? " ⚠ 可能震盪" : ""}</span>
            {!!state.agent_failure_streak && <span key={`${state.agent_failure_streak}-${state.agent_backoff_seconds}`} className={`chip warning${pulse.has("health") ? " status-pulse" : ""}`}>Agent 異常 {state.agent_failure_streak}{state.agent_backoff_seconds ? ` · ${state.agent_backoff_seconds} 秒後重試` : ""}</span>}
            {!!state.issues?.length && <button type="button" className="chip issue-chip" onClick={() => setIssuesOpen(true)}>⚠ issues {state.issues.length}</button>}
          </div>
        </div>
        {state.goal_changed && <div className="goal-warning">⚠ goal 已變更，建議回規劃期重新收斂</div>}
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
      {issuesOpen && workspace && <IssuesModal workspace={workspace.name} issues={state.issues ?? []} readonly={readonly || workspace.running} onClose={() => setIssuesOpen(false)} onChanged={onRefresh} />}
      {configOpen && workspace && <ConfigModal workspace={workspace.name} config={state.config ?? {}} onClose={() => setConfigOpen(false)} onChanged={onRefresh} />}
      {dialog && <ActionDialog title={dialog.title} message={dialog.message} confirmLabel={dialog.confirmLabel} onClose={() => setDialog(null)} onConfirm={dialog.onConfirm} />}
    </section>
  );
}
