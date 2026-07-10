import { useMemo, useState } from "react";
import type { FleetHistoryEntry, WorkspaceSummary } from "../../shared/api/types";
import { deriveFleetEvents } from "./fleetEvents";

const PHASE_NAMES: Record<string, string> = { plan: "規劃期", exec: "執行期", done: "🏁 完成" };
type FleetFilter = "all" | "attention" | "running" | "done";

function needsAttention(workspace: WorkspaceSummary): boolean {
  return !!(
    (workspace.red_streak ?? 0) > 0 ||
    (workspace.stall_rounds ?? 0) > 0 ||
    (workspace.issues ?? 0) > 0 ||
    (workspace.agent_failure_streak ?? 0) > 0 ||
    (workspace.state_recovery_count ?? 0) > 0 ||
    workspace.state_recovery_pending ||
    workspace.goal_changed ||
    workspace.stale_loop_pid
  );
}

function progress(workspace: WorkspaceSummary): { done: number; total: number; pct: number } {
  const total = workspace.plan_len ?? 0;
  const done = workspace.completed ?? 0;
  return { done, total, pct: total > 0 ? Math.round((done / total) * 100) : 0 };
}

function currentActivity(workspace: WorkspaceSummary): string {
  if (workspace.phase === "done") return "全部任務收斂完成";
  if (workspace.phase === "plan") return workspace.running ? "規劃收斂中…" : "規劃期（已停止）";
  if (workspace.current_task) return `task-${workspace.current_order}：${workspace.current_task}`;
  return "";
}

/** 監控電視牆:聚合統計 + 全 fleet 即時卡片 + 事件推播。
 * 卡片與歷史事件都走同一條 SSE；事件流仍由前端從 history 尾段推導。 */
export default function FleetOverview({ workspaces, fleetHistory, onSelect }: {
  workspaces: WorkspaceSummary[];
  fleetHistory: FleetHistoryEntry[];
  onSelect: (name: string) => void;
}) {
  const events = useMemo(() => deriveFleetEvents(fleetHistory), [fleetHistory]);
  const [filter, setFilter] = useState<FleetFilter>("all");
  const [search, setSearch] = useState("");

  const running = workspaces.filter((workspace) => workspace.running).length;
  const done = workspaces.filter((workspace) => workspace.phase === "done").length;
  const planning = workspaces.filter((workspace) => workspace.phase === "plan").length;
  const executing = workspaces.filter((workspace) => workspace.phase === "exec").length;
  const totalTasks = workspaces.reduce((sum, workspace) => sum + (workspace.plan_len ?? 0), 0);
  const doneTasks = workspaces.reduce((sum, workspace) => sum + (workspace.completed ?? 0), 0);
  const alerts = workspaces.filter(needsAttention).length;
  const visibleWorkspaces = useMemo(() => {
    let visible = filter === "attention" ? workspaces.filter(needsAttention)
      : filter === "running" ? workspaces.filter((workspace) => workspace.running)
        : filter === "done" ? workspaces.filter((workspace) => workspace.phase === "done")
          : workspaces;
    const query = search.trim().toLowerCase();
    if (query) visible = visible.filter((workspace) => workspace.name.toLowerCase().includes(query));
    return visible;
  }, [filter, search, workspaces]);
  const filters: Array<{ id: FleetFilter; label: string; count: number }> = [
    { id: "all", label: "全部", count: workspaces.length },
    { id: "attention", label: "需關注", count: alerts },
    { id: "running", label: "執行中", count: running },
    { id: "done", label: "已完成", count: done },
  ];
  const taskPct = totalTasks > 0 ? Math.round((doneTasks / totalTasks) * 100) : 0;

  return (
    <main className="fleet-overview" aria-label="Fleet 總覽">
      <div className="fleet-stats" role="list" aria-label="Fleet 統計">
        <div className="fleet-stat" role="listitem"><strong>{workspaces.length}</strong><span>workspaces</span></div>
        <div className="fleet-stat running" role="listitem"><strong>{running}</strong><span>執行中</span></div>
        <div className="fleet-stat" role="listitem"><strong>{planning} / {executing} / {done}</strong><span>規劃 / 執行 / 完成</span></div>
        <div className={`fleet-stat${alerts > 0 ? " warning" : ""}`} role="listitem"><strong>{alerts}</strong><span>需要關注</span></div>
        <div className="fleet-stat tasks" role="listitem">
          <strong>{doneTasks} / {totalTasks}<em>（{taskPct}%）</em></strong>
          <span>任務完成</span>
          <div className="fleet-progress"><div className="fleet-progress-fill" style={{ width: `${taskPct}%` }} /></div>
        </div>
      </div>
      <div className="fleet-filter-row">
        <div className="fleet-filters" role="group" aria-label="Workspace 篩選">
          {filters.map((item) => (
            <button key={item.id} type="button" className={filter === item.id ? "active" : ""}
              aria-pressed={filter === item.id} onClick={() => setFilter(item.id)}>
              {item.label} <span>{item.count}</span>
            </button>
          ))}
        </div>
        <input className="fleet-search" type="search" aria-label="搜尋 workspace" placeholder="搜尋 workspace…"
          value={search} onChange={(event) => setSearch(event.target.value)} />
        <span className="muted">顯示 {visibleWorkspaces.length} / {workspaces.length}</span>
      </div>
      <div className="fleet-body">
        <div className="fleet-grid">
          {visibleWorkspaces.map((workspace) => {
            const { done: cardDone, total, pct } = progress(workspace);
            const alert = needsAttention(workspace);
            const activity = currentActivity(workspace);
            return (
              <button key={workspace.name} type="button" className={`fleet-card phase-${workspace.phase ?? "unknown"}${workspace.running ? " running" : ""}`} onClick={() => onSelect(workspace.name)}>
                <div className="fleet-card-head">
                  <strong>{workspace.name}</strong>
                  {workspace.running && <span className="breathing-dot" aria-label="執行中" />}
                </div>
                <div className="fleet-card-meta">
                  <span className={`phase-badge phase-${workspace.phase ?? "unknown"}`}>{PHASE_NAMES[workspace.phase ?? ""] ?? "—"}</span>
                  <span className="muted">round {workspace.round ?? 0}</span>
                  {workspace.phase === "plan" && <span className="muted">flag {workspace.flag ?? 0}</span>}
                  {workspace.phase === "exec" && <span className="muted">done {workspace.done_count ?? 0}</span>}
                </div>
                {total > 0 && workspace.phase !== "plan" && (
                  <div className="fleet-progress" aria-label={`任務 ${cardDone}/${total}`}>
                    <div className="fleet-progress-fill" style={{ width: `${pct}%` }} />
                    <span className="fleet-progress-text">{cardDone}/{total}</span>
                  </div>
                )}
                {activity && <div className="fleet-card-task" title={activity}>{workspace.phase === "exec" ? "→ " : ""}{activity}</div>}
                {alert && (
                  <div className="fleet-card-alerts">
                    {(workspace.red_streak ?? 0) > 0 && <span className="chip warning">紅連跳 {workspace.red_streak}</span>}
                    {(workspace.stall_rounds ?? 0) > 0 && <span className="chip subdued">停滯 {workspace.stall_rounds}</span>}
                    {(workspace.issues ?? 0) > 0 && <span className="chip issue-chip">issues {workspace.issues}</span>}
                    {(workspace.agent_failure_streak ?? 0) > 0 && <span className="chip warning">Agent 異常 {workspace.agent_failure_streak}</span>}
                    {(workspace.state_recovery_count ?? 0) > 0 && <span className="chip warning">🛟 state 復原 {workspace.state_recovery_count}</span>}
                    {workspace.state_recovery_pending && <span className="chip warning">🛟 checkpoint</span>}
                    {workspace.goal_changed && <span className="chip warning">goal 已變更</span>}
                    {workspace.stale_loop_pid && <span className="chip warning">⚠ PID 殘留</span>}
                  </div>
                )}
                {workspace.repo && <div className="fleet-card-repo" title={workspace.repo}>{workspace.repo}</div>}
              </button>
            );
          })}
          {!visibleWorkspaces.length && <div className="empty-inline">{search.trim() ? "沒有符合搜尋的 workspace" : filter === "all" ? "尚未建立 workspace" : "沒有符合目前篩選的 workspace"}</div>}
        </div>
        <aside className="fleet-events" aria-label="事件推播">
          <div className="fleet-events-head"><strong>事件推播</strong><span className="muted">最近 {events.length} 則</span></div>
          <div className="fleet-events-list">
            {events.map((event, index) => (
              <button key={`${event.ws}-${event.ts}-${index}`} type="button" className="fleet-event" onClick={() => onSelect(event.ws)}>
                <span className="fleet-event-time">{event.time}</span>
                <span className="fleet-event-ws">{event.ws}</span>
                <span className="fleet-event-text">{event.text}</span>
              </button>
            ))}
            {!events.length && <div className="empty-inline">尚無事件——loop 跑完第一輪後會出現在這裡。</div>}
          </div>
        </aside>
      </div>
    </main>
  );
}
