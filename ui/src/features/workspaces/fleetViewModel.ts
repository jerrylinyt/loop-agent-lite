/** Fleet 總覽的純資料模型：集中篩選、排序、統計與 localStorage 邊界驗證。 */
import type { WorkspaceSummary } from "../../shared/api/types";

export type FleetFilter = "all" | "attention" | "running" | "done";
export type FleetSort = "name" | "attention" | "running" | "progress";

const FLEET_FILTERS: FleetFilter[] = ["all", "attention", "running", "done"];
const FLEET_SORTS: FleetSort[] = ["name", "attention", "running", "progress"];

export function workspaceNeedsAttention(workspace: WorkspaceSummary): boolean {
  // done workspace 不再因歷史紅燈/停滯誤報；持續存在的錯誤與人工待辦仍要顯示。
  const completed = workspace.phase === "done";
  return !!(
    workspace.error ||
    (workspace.unread_issues ?? workspace.issues ?? 0) > 0 ||
    workspace.state_recovery_pending ||
    workspace.goal_changed ||
    workspace.stale_loop_pid ||
    (!completed && (
      (workspace.red_streak ?? 0) > 0 ||
      (workspace.stall_rounds ?? 0) > 0 ||
      (workspace.agent_failure_streak ?? 0) > 0 ||
      workspace.last_round_timed_out ||
      (workspace.state_recovery_count ?? 0) > 0
    ))
  );
}

export function initialFleetFilter(): FleetFilter {
  const saved = localStorage.getItem("fleet-filter") as FleetFilter | null;
  return saved && FLEET_FILTERS.includes(saved) ? saved : "all";
}

export function initialFleetSort(): FleetSort {
  const saved = localStorage.getItem("fleet-sort") as FleetSort | null;
  return saved && FLEET_SORTS.includes(saved) ? saved : "name";
}

export function formatMetric(seconds: number | null): string {
  if (seconds === null) return "—";
  return `${seconds < 1 ? seconds.toFixed(2) : seconds.toFixed(1)}s`;
}

export function taskProgress(workspace: WorkspaceSummary): { done: number; total: number; pct: number } {
  const total = workspace.plan_len ?? 0;
  const done = workspace.completed ?? 0;
  return { done, total, pct: total > 0 ? Math.round((done / total) * 100) : 0 };
}

export function currentActivity(workspace: WorkspaceSummary): string {
  if (workspace.error) return "state 讀取失敗，請檢查 checkpoint 或重新啟動";
  if (workspace.phase === "done") return "全部任務收斂完成";
  if (workspace.phase === "plan") return workspace.running ? "規劃收斂中…" : "規劃期（已停止）";
  if (workspace.current_task) return `task-${workspace.current_order}：${workspace.current_task}`;
  return "";
}

export function visibleFleetWorkspaces(
  workspaces: WorkspaceSummary[], filter: FleetFilter, search: string, sort: FleetSort
): WorkspaceSummary[] {
  let visible = filter === "attention" ? workspaces.filter(workspaceNeedsAttention)
    : filter === "running" ? workspaces.filter((workspace) => workspace.running)
      : filter === "done" ? workspaces.filter((workspace) => workspace.phase === "done")
        : workspaces;
  const query = search.trim().toLowerCase();
  if (query) visible = visible.filter((workspace) => workspace.name.toLowerCase().includes(query));
  return [...visible].sort((left, right) => {
    if (sort === "attention") return Number(workspaceNeedsAttention(right)) - Number(workspaceNeedsAttention(left)) || left.name.localeCompare(right.name);
    if (sort === "running") return Number(right.running) - Number(left.running) || left.name.localeCompare(right.name);
    if (sort === "progress") return taskProgress(right).pct - taskProgress(left).pct || left.name.localeCompare(right.name);
    return left.name.localeCompare(right.name);
  });
}
