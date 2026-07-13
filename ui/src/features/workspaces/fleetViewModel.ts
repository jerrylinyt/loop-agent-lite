/** Fleet 總覽的純資料模型：集中篩選、排序、統計與 localStorage 邊界驗證。 */
import type { WorkspaceSummary } from "../../shared/api/types";
import { parallelNeedsAttention, parallelPhaseLabel } from "./parallelPhase";

export type FleetFilter = "all" | "attention" | "running" | "done";
export type FleetSort = "name" | "attention" | "running" | "progress";
export interface SavedFleetView {
  id: string;
  name: string;
  filter: FleetFilter;
  search: string;
  sort: FleetSort;
  compact: boolean;
}

const FLEET_FILTERS: FleetFilter[] = ["all", "attention", "running", "done"];
const FLEET_SORTS: FleetSort[] = ["name", "attention", "running", "progress"];

export function isFleetChildOfParent(child: WorkspaceSummary, parent: WorkspaceSummary | undefined): boolean {
  if (child.workspace_kind !== "fleet-child" || parent?.workspace_kind !== "fleet-parent" ||
      !parent.fleet_run_id || child.fleet_parent !== parent.name ||
      child.fleet_run_id !== parent.fleet_run_id || !child.track) return false;
  // The parent's track registry is authoritative. A same-run child that is not registered there
  // is an orphan/corrupt diagnostic workspace and must remain independently visible.
  return (parent.parallel_tracks ?? []).some((track) =>
    track.name === child.track && track.child_workspace === child.name
  );
}

export function fleetChildNamesForParent(parent: WorkspaceSummary, workspaces: WorkspaceSummary[]): string[] {
  return workspaces.filter((workspace) => isFleetChildOfParent(workspace, parent)).map((workspace) => workspace.name);
}

export function overviewWorkspaceProjection(workspaces: WorkspaceSummary[]): WorkspaceSummary[] {
  const byName = new Map(workspaces.map((workspace) => [workspace.name, workspace]));
  // 只有 parent kind 與 run identity 都吻合的 child 才可聚合；同名 standalone、舊 run child
  // 與 parent 遺失的 orphan 都保留獨立卡片供診斷。
  return workspaces.filter((workspace) => !isFleetChildOfParent(
    workspace,
    workspace.fleet_parent ? byName.get(workspace.fleet_parent) : undefined
  ));
}

export function workspaceNeedsAttention(workspace: WorkspaceSummary): boolean {
  // done workspace 不再因歷史紅燈/停滯誤報；持續存在的錯誤與人工待辦仍要顯示。
  const completed = workspace.phase === "done" || workspace.parallel_phase === "done";
  const parallelAttention = workspace.workspace_kind === "fleet-parent" &&
    parallelNeedsAttention(workspace.parallel_phase, workspace.parallel_tracks);
  return !!(
    workspace.error ||
    (workspace.unread_issues ?? workspace.issues ?? 0) > 0 ||
    workspace.state_recovery_pending ||
    workspace.goal_changed ||
    workspace.stale_loop_pid ||
    parallelAttention ||
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

export function loadSavedViews(): SavedFleetView[] {
  // 個人視圖不是 coordinator truth；解析失敗直接回空，且最多載入 20 組。
  try {
    const value = JSON.parse(localStorage.getItem("fleet-saved-views") ?? "[]") as unknown;
    if (!Array.isArray(value)) return [];
    return value.filter((item): item is SavedFleetView => !!item && typeof item === "object" &&
      typeof (item as SavedFleetView).id === "string" && typeof (item as SavedFleetView).name === "string" &&
      FLEET_FILTERS.includes((item as SavedFleetView).filter) && FLEET_SORTS.includes((item as SavedFleetView).sort) &&
      typeof (item as SavedFleetView).search === "string" && typeof (item as SavedFleetView).compact === "boolean").slice(0, 20);
  } catch {
    return [];
  }
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
  if (workspace.parallel_phase === "done" || workspace.phase === "done") return "全部任務收斂完成";
  if (workspace.workspace_kind === "fleet-parent") return `Parallel run：${parallelPhaseLabel(workspace.parallel_phase)}`;
  if (workspace.phase === "plan") return workspace.running ? "規劃收斂中…" : "規劃期（已停止）";
  if (workspace.current_task) return `task-${workspace.current_order}：${workspace.current_task}`;
  return "";
}

export function visibleFleetWorkspaces(
  workspaces: WorkspaceSummary[], filter: FleetFilter, search: string, sort: FleetSort
): WorkspaceSummary[] {
  let visible = filter === "attention" ? workspaces.filter(workspaceNeedsAttention)
    : filter === "running" ? workspaces.filter((workspace) => workspace.running)
      : filter === "done" ? workspaces.filter((workspace) => workspace.phase === "done" || workspace.parallel_phase === "done")
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
