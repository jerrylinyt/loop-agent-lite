import { useState } from "react";
import { postJson } from "../../shared/api/client";
import type { PlanTask, WorkspaceState, WorkspaceSummary } from "../../shared/api/types";
import ConfigModal from "./ConfigModal";
import IssuesModal from "./IssuesModal";
import PlanTable from "./PlanTable";
import RecentEvents from "./RecentEvents";

const PHASE_NAMES = { plan: "規劃期", exec: "執行期", done: "🏁 完成" };

export default function WorkspaceView({
  workspace,
  state,
  readonly,
  events,
  onRefresh,
  onRefreshWorkspaces
}: {
  workspace?: WorkspaceSummary;
  state: WorkspaceState | null;
  readonly: boolean;
  events: string[];
  onRefresh: () => void;
  onRefreshWorkspaces: () => void;
}) {
  const [configOpen, setConfigOpen] = useState(false);
  const [issuesOpen, setIssuesOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const canChange = !!workspace && !readonly && !workspace.running;

  if (!state) return <section className="workspace-pane"><div className="loading-state">載入 workspace…</div></section>;
  if (state.error) return <section className="workspace-pane"><div className="loading-state error">{state.error === "busy" ? "state 更新中…" : state.error}</div></section>;

  const mutate = async (url: string, body: unknown) => {
    setBusy(true);
    const response = await postJson(url, body);
    setBusy(false);
    if (response.error) return alert(response.error);
    onRefresh();
    onRefreshWorkspaces();
  };
  const changePhase = (phase: "plan" | "exec") => {
    const message = phase === "exec"
      ? "直接進入執行期，從第一個任務開始。繼續？"
      : "回到規劃期會清除執行進度與完成紀錄，計畫保留。繼續？";
    if (confirm(message)) void mutate("/api/phase", { name: workspace?.name, phase });
  };
  const gotoTask = (order: number) => {
    const done = new Set((state.completed ?? []).map((entry) => entry.order));
    const skipped = (state.plan ?? []).map((task) => task.order).filter((value) => value < order && !done.has(value));
    const message = skipped.length
      ? `跳到 task-${order}：task ${skipped.join(", ")} 會標記為人工確認完成，並先跑 validate。繼續？`
      : `退回 task-${order}：這個任務以後的完成紀錄會清除，code 不會動。繼續？`;
    if (confirm(message)) void mutate("/api/set-task", { name: workspace?.name, order });
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
          <div className="workspace-title"><h1>{workspace?.name ?? "workspace"}</h1><span className={`phase-badge phase-${state.phase}`}>{PHASE_NAMES[state.phase]}</span></div>
          {!readonly && workspace && <div className="workspace-actions">
            <button type="button" className={workspace.running ? "danger-button" : "success-button"} disabled={busy} onClick={() => void mutate(workspace.running ? "/api/stop" : "/api/run", { name: workspace.name })}>{workspace.running ? "⏹ 停止" : "▶ 運行"}</button>
            {canChange && state.phase === "plan" && total > 0 && <button type="button" className="secondary-button" onClick={() => changePhase("exec")}>⏩ 進執行期</button>}
            {canChange && (state.phase === "exec" || state.phase === "done") && <button type="button" className="secondary-button" onClick={() => changePhase("plan")}>⏪ 回規劃期</button>}
            {canChange && <button type="button" className="secondary-button" onClick={() => setConfigOpen(true)}>⚙ 設定</button>}
          </div>}
        </div>
        <div className="workspace-status-row">
          <div className="primary-status">
            <span className="chip">round {state.round}</span>
            {state.phase !== "plan" && total > 0 && <span className="chip">任務 {completed}/{total}</span>}
            {state.phase === "plan" && <span className="chip">flag {state.flag} / &gt;{state.config?.flag_threshold ?? 10}</span>}
            {state.phase === "exec" && <span className="chip">done {state.done_count} / ≥{state.config?.done_threshold ?? 3}</span>}
          </div>
          <div className="health-status">
            <span className={`chip subdued${state.phase === "plan" && state.plan_version >= 10 ? " warning" : ""}`}>紅連跳 {state.red_streak} · 停滯 {state.stall_rounds} · plan v{state.plan_version}{state.phase === "plan" && state.plan_version >= 10 ? " ⚠ 可能震盪" : ""}</span>
            {!!state.issues?.length && <button type="button" className="chip issue-chip" onClick={() => setIssuesOpen(true)}>⚠ issues {state.issues.length}</button>}
          </div>
        </div>
        {state.goal_changed && <div className="goal-warning">⚠ goal 已變更，建議回規劃期重新收斂</div>}
      </header>
      <PlanTable state={state} canEdit={canChange} onSave={savePlan} onGoto={gotoTask} />
      <RecentEvents lines={events} />
      {issuesOpen && workspace && <IssuesModal workspace={workspace.name} issues={state.issues ?? []} readonly={readonly || workspace.running} onClose={() => setIssuesOpen(false)} onChanged={onRefresh} />}
      {configOpen && workspace && <ConfigModal workspace={workspace.name} config={state.config ?? {}} onClose={() => setConfigOpen(false)} onChanged={onRefresh} />}
    </section>
  );
}
