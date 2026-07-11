import type { WorkspaceSummary } from "../../shared/api/types";

export type DiagnosticSeverity = "error" | "warning" | "info";

export interface WorkspaceDiagnostic {
  id: string;
  severity: DiagnosticSeverity;
  title: string;
  evidence: string;
  recommendation: string;
}

export function workspaceDiagnostics(workspace: WorkspaceSummary): WorkspaceDiagnostic[] {
  const diagnostics: WorkspaceDiagnostic[] = [];
  const completed = workspace.phase === "done";
  const add = (diagnostic: WorkspaceDiagnostic) => diagnostics.push(diagnostic);
  if (workspace.error) add({
    id: "state-error", severity: "error", title: "State 無法讀取",
    evidence: workspace.error,
    recommendation: "檢查 state.json 與 last-good checkpoint；修復前不要重啟 loop。"
  });
  const unreadIssues = workspace.unread_issues ?? workspace.issues ?? 0;
  if (unreadIssues > 0) add({
    id: "issues", severity: "warning", title: `${unreadIssues} 條 issue 尚未讀取`,
    evidence: workspace.latest_issue ? `最新回報：${workspace.latest_issue}` : `目前保留 ${workspace.issues ?? unreadIssues} 條 Agent 結構化回報。`,
    recommendation: "進入 workspace 開啟 Issues，確認阻擋原因後標記已讀或清除。"
  });
  if (workspace.state_recovery_pending) add({
    id: "checkpoint", severity: "error", title: "正從 checkpoint 唯讀顯示",
    evidence: "primary state 不可讀，Dashboard 沒有修改原檔。",
    recommendation: "停止寫入操作，先確認 checkpoint，再用目前版本啟動以執行受控復原。"
  });
  if (workspace.goal_changed) add({
    id: "goal", severity: "warning", title: "Goal 已在計畫收斂後變更",
    evidence: "目前 plan 可能仍以舊 goal 為基準。",
    recommendation: "檢視 Goal diff，確認後回規劃期重新收斂。"
  });
  if (workspace.stale_loop_pid) add({
    id: "stale-pid", severity: "warning", title: "State 留有失效 PID",
    evidence: `PID ${workspace.loop_pid ?? "?"}${workspace.loop_started_at ? `，啟動於 ${workspace.loop_started_at}` : ""}，但程序已不存在。`,
    recommendation: "確認沒有外部 loop 後，以 Dashboard 重新運行；不要手動刪除 lock。"
  });
  if (!completed && (workspace.red_streak ?? 0) > 0) add({
    id: "red", severity: "warning", title: `Validate 連續紅燈 ${workspace.red_streak} 輪`,
    evidence: `距 reset 防線仍由 workspace 的 red-limit 控制。`,
    recommendation: "查看輪次紀錄與 Validate 尾段，先修第一個失敗再繼續。"
  });
  if (!completed && (workspace.stall_rounds ?? 0) > 0) add({
    id: "stall", severity: "info", title: `HEAD 已停滯 ${workspace.stall_rounds} 輪`,
    evidence: workspace.current_task ? `目前 task-${workspace.current_order}：${workspace.current_task}` : "目前沒有可投影的任務文字。",
    recommendation: "確認 Agent 是否只重複分析；必要時檢查 prompt、issue 與任務描述。"
  });
  if (!completed && (workspace.agent_failure_streak ?? 0) > 0) add({
    id: "agent", severity: "error", title: `Agent CLI 連續異常 ${workspace.agent_failure_streak} 輪`,
    evidence: workspace.agent_backoff_seconds ? `目前退避 ${workspace.agent_backoff_seconds} 秒。` : "目前沒有退避等待。",
    recommendation: "查看 Agent log，確認 CLI、認證、PATH 與模型服務是否正常。"
  });
  if (!completed && workspace.last_round_timed_out) add({
    id: "timeout", severity: "warning", title: "最近一輪 Agent 逾時",
    evidence: `最近一輪耗時 ${workspace.last_round_seconds ?? "?"} 秒。`,
    recommendation: "檢查任務是否過大或 CLI 卡住，再決定調整任務或 round timeout。"
  });
  if (!completed && (workspace.state_recovery_count ?? 0) > 0) add({
    id: "recovery", severity: "info", title: `State 曾復原 ${workspace.state_recovery_count} 次`,
    evidence: "復原次數保留作為目前 run 的稽核資訊。",
    recommendation: "若持續增加，檢查外部程序是否直接修改 coordinator state。"
  });
  return diagnostics;
}

export function workspaceNeedsAttention(workspace: WorkspaceSummary): boolean {
  return workspaceDiagnostics(workspace).length > 0;
}
