/** Minimal Parallel Loop view: base status, batches/tasks, and correctly routed controls. */
import { lazy, Suspense, useMemo, useState } from "react";
import { postJson, waitForJobStartup } from "../../shared/api/client";
import type { ParallelRunStatus, StartupResponse, WorkspaceState, WorkspaceSummary } from "../../shared/api/types";
import ActionDialog from "../../shared/components/ActionDialog";
import HistoryModal from "./HistoryModal";

const TaskDiffModal = lazy(() => import("./TaskDiffModal"));

const STATUS_LABELS: Record<ParallelRunStatus, string> = {
  initializing: "初始化",
  running: "執行中",
  pause_requested: "暫停收尾中",
  paused: "已暫停",
  cancel_requested: "取消收尾中",
  finalizing: "完成收尾中",
  finalizing_cancel: "取消清理中",
  blocked: "已阻擋",
  completed: "已完成",
  cancelled: "已取消",
};

const OUTCOME_LABELS: Record<string, string> = {
  pending: "等待",
  integrated: "已整合",
  blocked: "阻擋",
  cancelled: "取消",
};

export default function ParallelView({ workspace, state, readonly, onRefresh, onRefreshWorkspaces }: {
  workspace?: WorkspaceSummary;
  state: WorkspaceState;
  readonly: boolean;
  onRefresh: () => void | Promise<void>;
  onRefreshWorkspaces: () => void | Promise<void>;
}) {
  const [busy, setBusy] = useState<"pause" | "resume" | "abort" | "delete" | null>(null);
  const [message, setMessage] = useState("");
  const [confirmAbort, setConfirmAbort] = useState(false);
  const [confirmDelete, setConfirmDelete] = useState(false);
  const [workerHistoryOpen, setWorkerHistoryOpen] = useState(false);
  const [diffTask, setDiffTask] = useState<{ order: number; title: string; sha: string } | null>(null);
  const completedByOrder = useMemo(
    () => new Map((state.completed ?? []).map((entry) => [entry.order, entry])),
    [state.completed]
  );
  const statusByOrder = useMemo(
    () => new Map((state.parallel?.tasks ?? []).map((task) => [task.order, task])),
    [state.parallel?.tasks]
  );

  if (state.runner === "parallel-worker" || state.managed_readonly) {
    const assigned = state.plan?.find((task) => task.order === state.assigned_order);
    return (
      <section className="workspace-pane parallel-workspace-pane">
        <header className="workspace-header">
          <div className="workspace-title-row"><div className="workspace-title">
            <h1>{workspace?.name ?? `task-${state.assigned_order ?? "?"}`}</h1>
            <span className="phase-badge parallel-runner-tag">Managed Worker</span>
          </div>{workspace && <div className="workspace-actions"><button type="button" className="secondary-button" onClick={() => setWorkerHistoryOpen(true)}>歷史</button></div>}</div>
          <div className="workspace-status-row"><div className="primary-status">
            <span className="chip">parent {state.parent_workspace ?? "—"}</span>
            <span className="chip subdued">run {(state.run_id ?? "—").slice(0, 12)}</span>
            <span className="chip">task-{state.assigned_order ?? "?"}</span>
            <span className="chip subdued">{state.assignment?.status ?? "unknown"}</span>
          </div></div>
          <div className="sr-only" role="status" aria-live="polite">
            Managed worker task-{state.assigned_order ?? "?"} 狀態 {state.assignment?.status ?? "unknown"}
          </div>
          <p className="parallel-readonly-note">此 workspace 由 parent supervisor 管理，只提供唯讀狀態與 console。</p>
        </header>
        <section className="parallel-task-panel">
          <header className="pane-header"><div><strong>Frozen plan（唯讀）</strong><span>目前指派 task-{state.assigned_order ?? "?"}</span></div></header>
          <div className="table-scroll"><table><thead><tr><th>#</th><th>Batch</th><th>任務</th><th>狀態</th></tr></thead><tbody>
            {assigned && <tr key={assigned.order} className="current"><td>{assigned.order}</td><td>{assigned.stack ?? "—"}</td><td>{assigned.task}</td><td>{state.assignment?.status ?? "running"}</td></tr>}
          </tbody></table></div>
          {!assigned && <p className="field-error">找不到 frozen plan 中的指派任務</p>}
          {state.assignment?.exit_reason && <p className="field-error" role="alert">{state.assignment.exit_reason}</p>}
        </section>
        {workerHistoryOpen && workspace && <HistoryModal workspace={workspace.name} onClose={() => setWorkerHistoryOpen(false)} />}
      </section>
    );
  }

  const parallel = state.parallel ?? {};
  const status = parallel.status;
  const mutate = async (action: "pause" | "resume" | "abort") => {
    if (!workspace) return;
    setBusy(action);
    setMessage("");
    try {
      const endpoint = action === "pause" ? "/api/stop" : action === "resume" ? "/api/resume" : "/api/abort";
      const response = await postJson<StartupResponse>(endpoint, { name: workspace.name });
      if (response.error) {
        setMessage(`錯誤：${response.error}`);
        return;
      }
      if (response.starting) {
        if (!response.job_id && (!response.name || !response.pid)) {
          setMessage("錯誤：啟動回應缺少 job_id 或 name/pid");
          return;
        }
        const startup = await waitForJobStartup(
          response.name ?? workspace.name,
          response.pid ?? 0,
          response.startup_timeout ?? 30,
          response.job_id
        );
        if (startup.error) {
          setMessage(`錯誤：${startup.error}`);
          return;
        }
      }
      setMessage(action === "pause"
        ? "已送出 Pause"
        : action === "resume"
          ? terminalIntent === "cancelled"
            ? "已啟動取消清理"
            : status === "finalizing" ? "已啟動完成收尾" : "已啟動 Resume"
          : "已送出 Abort");
      await Promise.all([Promise.resolve(onRefresh()), Promise.resolve(onRefreshWorkspaces())]);
    } finally {
      setBusy(null);
    }
  };
  const terminalIntent = parallel.terminal_intent ?? null;
  const pauseRecovery = status === "pause_requested";
  const completionRecovery = status === "finalizing"
    && terminalIntent === "completed"
    && workspace?.running === false;
  const cancelRecovery = terminalIntent === "cancelled"
    && (status === "cancel_requested" || status === "finalizing_cancel")
    && workspace?.running === false;
  const canPause = status === "initializing" || status === "running" || pauseRecovery;
  const canResume = status === "paused" || status === "blocked" || completionRecovery || cancelRecovery;
  const canAbort = terminalIntent === null && (
    status === "initializing"
    || status === "running"
    || status === "pause_requested"
    || status === "paused"
    || status === "blocked"
  );
  const canDelete = !workspace?.running && (status === "completed" || status === "cancelled");
  const pauseLabel = pauseRecovery ? "重試 Pause" : "Pause";
  const resumeLabel = (status === "blocked" || status === "finalizing") && terminalIntent === "completed"
    ? "重試完成收尾"
    : terminalIntent === "cancelled" && (status === "blocked" || cancelRecovery)
      ? "重試取消清理"
      : "Resume";
  const titleByOrder = new Map((state.plan ?? []).map((task) => [task.order, task.task]));

  return (
    <section className="workspace-pane parallel-workspace-pane">
      <header className="workspace-header parallel-header">
        <div className="workspace-title-row">
          <div className="workspace-title"><h1>{workspace?.name ?? "workspace"}</h1><span className="phase-badge parallel-runner-tag">Parallel</span></div>
          {!readonly && workspace && <div className="workspace-actions">
            <button type="button" className="secondary-button" disabled={!canPause || busy !== null} onClick={() => void mutate("pause")}>{busy === "pause" ? `${pauseLabel} 中…` : pauseLabel}</button>
            <button type="button" className="success-button" disabled={!canResume || busy !== null} onClick={() => void mutate("resume")}>{busy === "resume" ? `${resumeLabel}中…` : resumeLabel}</button>
            <button type="button" className="danger-button" disabled={!canAbort || busy !== null} onClick={() => setConfirmAbort(true)}>{busy === "abort" ? "Abort 中…" : "Abort"}</button>
            {canDelete && <button type="button" className="danger-button" disabled={busy !== null} onClick={() => setConfirmDelete(true)}>刪除</button>}
          </div>}
        </div>
        <div className="workspace-status-row"><div className="primary-status">
          <span className={`chip parallel-status-${status ?? "unknown"}`}>{status ? STATUS_LABELS[status] : "狀態未知"}</span>
          <span className="chip subdued">run {(parallel.run_id ?? "—").slice(0, 12)}</span>
          <span className="chip">batch {parallel.batch ?? "—"}</span>
          <span className="chip">任務 {completedByOrder.size}/{state.plan?.length ?? 0}</span>
        </div></div>
        <div className="sr-only" role="status" aria-live="polite">
          Parallel 狀態 {status ? STATUS_LABELS[status] : "未知"}，batch {parallel.batch ?? "未知"}，已完成 {completedByOrder.size}/{state.plan?.length ?? 0} 個任務
        </div>
        {parallel.error && <div className="goal-warning" role="alert">{parallel.error}</div>}
        {message && <div
          className="parallel-control-message"
          role={message.startsWith("錯誤：") ? "alert" : "status"}
          aria-live={message.startsWith("錯誤：") ? "assertive" : "polite"}
        >{message}</div>}
      </header>
      <section className="parallel-task-panel">
        <header className="pane-header"><div><strong>Parallel tasks</strong><span>依 frozen plan 與 durable supervisor 狀態投影</span></div></header>
        <div className="table-scroll"><table className="parallel-task-table">
          <thead><tr><th>#</th><th>Batch</th><th>任務</th><th>Outcome</th><th>Resource</th><th>重啟</th></tr></thead>
          <tbody>{(state.plan ?? []).map((task) => {
            const taskStatus = statusByOrder.get(task.order);
            const completed = completedByOrder.get(task.order);
            return <tr key={task.order} className={taskStatus?.outcome === "integrated" ? "completed" : ""}>
              <td>{task.order}</td><td>{taskStatus?.batch ?? task.stack ?? "—"}</td>
              <td className="task-cell"><strong>{task.task}</strong>{task.ref && <div className="task-ref">ref: {task.ref}</div>}{taskStatus?.error && <div className="field-error" role="alert">{taskStatus.error}</div>}</td>
              <td><span className={`chip parallel-outcome-${taskStatus?.outcome ?? "pending"}`}>{OUTCOME_LABELS[taskStatus?.outcome ?? "pending"] ?? taskStatus?.outcome}</span>{completed && <button type="button" className="commit-sha-button" title={`查看 task-${task.order} 的完整 Git 變更`} aria-label={`查看 task-${task.order} Git 變更 ${completed.sha.slice(0, 8)}`} onClick={() => setDiffTask({ order: task.order, title: titleByOrder.get(task.order) ?? `task-${task.order}`, sha: completed.sha })}>{completed.sha.slice(0, 8)}</button>}</td>
              <td>{taskStatus?.resource_state ?? "queued"}</td><td>{taskStatus?.restart_count ?? 0}</td>
            </tr>;
          })}</tbody>
        </table></div>
      </section>
      {confirmAbort && <ActionDialog
        title="確認 Abort Parallel run"
        message="Abort 會停止 workers，保留已整合 commits，並清理可安全移除的 worktrees；此操作不可用普通 Resume 復原。"
        confirmLabel="Abort"
        danger preview={[
        { label: "run", value: parallel.run_id ?? "—" },
        { label: "目前狀態", value: status ?? "unknown" },
        { label: "已整合", value: `${completedByOrder.size} tasks`, tone: "safe" },
      ]} onClose={() => setConfirmAbort(false)} onConfirm={() => {
        setConfirmAbort(false);
        if (!canAbort) {
          setMessage("錯誤：Parallel 狀態已變更，請重新確認後再操作");
          return;
        }
        void mutate("abort");
      }} />}
      {confirmDelete && workspace && <ActionDialog
        title="確認刪除 Parallel workspace"
        message={`永久刪除 ${workspace.name}？整個 workspace 與 durable run artifacts 都會直接移除，無法復原；target repo 與已整合 commits 不受影響。`}
        confirmLabel="永久刪除"
        danger preview={[
          { label: "永久刪除", value: `workspace/${workspace.name}`, tone: "warning" },
          { label: "run", value: parallel.run_id ?? "—" },
          { label: "終態", value: status ?? "unknown" },
          { label: "不受影響", value: state.config?.repo ? `target repo：${state.config.repo}` : "target repo 與已整合 commits", tone: "safe" },
        ]} onClose={() => setConfirmDelete(false)} onConfirm={() => {
          setConfirmDelete(false);
          if (!canDelete) {
            setMessage("錯誤：Parallel 狀態已變更，請重新確認後再操作");
            return;
          }
          setBusy("delete");
          setMessage("");
          void (async () => {
            try {
              const response = await postJson<{ ok?: boolean; deleted?: boolean }>("/api/delete-workspace", { name: workspace.name });
              if (response.error) {
                setMessage(`錯誤：${response.error}`);
                return;
              }
              await Promise.resolve(onRefreshWorkspaces());
            } finally {
              setBusy(null);
            }
          })();
        }} />}
      {diffTask && <Suspense fallback={null}><TaskDiffModal workspace={workspace?.name ?? ""} order={diffTask.order} fallbackTitle={diffTask.title} fallbackSha={diffTask.sha} onClose={() => setDiffTask(null)} /></Suspense>}
    </section>
  );
}
