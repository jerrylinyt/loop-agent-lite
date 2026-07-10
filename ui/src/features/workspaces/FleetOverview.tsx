import { useEffect, useMemo, useState } from "react";
import type { FleetHistoryEntry, FleetRoundMetrics, WorkspaceSummary } from "../../shared/api/types";
import { deriveFleetEvents } from "./fleetEvents";
import { deriveRoundTiming, useRoundNow } from "./roundTiming";

const PHASE_NAMES: Record<string, string> = { plan: "規劃期", exec: "執行期", done: "🏁 完成" };
type FleetFilter = "all" | "attention" | "running" | "done";
const FLEET_FILTERS: FleetFilter[] = ["all", "attention", "running", "done"];

function initialFleetFilter(): FleetFilter {
  const saved = localStorage.getItem("fleet-filter") as FleetFilter | null;
  return saved && FLEET_FILTERS.includes(saved) ? saved : "all";
}

function needsAttention(workspace: WorkspaceSummary): boolean {
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

function formatMetric(seconds: number | null): string {
  if (seconds === null) return "—";
  return `${seconds < 1 ? seconds.toFixed(2) : seconds.toFixed(1)}s`;
}

function progress(workspace: WorkspaceSummary): { done: number; total: number; pct: number } {
  const total = workspace.plan_len ?? 0;
  const done = workspace.completed ?? 0;
  return { done, total, pct: total > 0 ? Math.round((done / total) * 100) : 0 };
}

function currentActivity(workspace: WorkspaceSummary): string {
  if (workspace.error) return "state 讀取失敗，請檢查 checkpoint 或重新啟動";
  if (workspace.phase === "done") return "全部任務收斂完成";
  if (workspace.phase === "plan") return workspace.running ? "規劃收斂中…" : "規劃期（已停止）";
  if (workspace.current_task) return `task-${workspace.current_order}：${workspace.current_task}`;
  return "";
}

/** 監控電視牆:聚合統計 + 全 fleet 即時卡片 + 事件推播。
 * 卡片與歷史事件都走同一條 SSE；事件流仍由前端從 history 尾段推導。 */
export default function FleetOverview({ workspaces, fleetHistory, fleetMetrics, attentionRequest, onSelect }: {
  workspaces: WorkspaceSummary[];
  fleetHistory: FleetHistoryEntry[];
  fleetMetrics: FleetRoundMetrics | null;
  attentionRequest: number;
  onSelect: (name: string) => void;
}) {
  const events = useMemo(() => deriveFleetEvents(fleetHistory), [fleetHistory]);
  const metricsByWorkspace = useMemo(
    () => new Map(fleetHistory.map((entry) => [entry.name, entry.metrics])),
    [fleetHistory]
  );
  const roundNow = useRoundNow(workspaces.some((workspace) =>
    Boolean(workspace.round_started_at && !workspace.round_interrupted_at)));
  const [filter, setFilter] = useState<FleetFilter>(initialFleetFilter);
  const [search, setSearch] = useState(() => localStorage.getItem("fleet-search") ?? "");

  useEffect(() => {
    if (attentionRequest > 0) {
      setFilter("attention");
      setSearch("");
      localStorage.setItem("fleet-filter", "attention");
      localStorage.setItem("fleet-search", "");
    }
  }, [attentionRequest]);

  const changeFilter = (next: FleetFilter) => {
    setFilter(next);
    localStorage.setItem("fleet-filter", next);
  };
  const changeSearch = (next: string) => {
    setSearch(next);
    localStorage.setItem("fleet-search", next);
  };

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
    <main className="fleet-overview" aria-label="工作區總覽">
      <div className="fleet-stats" role="list" aria-label="工作區統計">
        <div className="fleet-stat" role="listitem"><strong>{workspaces.length}</strong><span>workspaces</span></div>
        <div className="fleet-stat running" role="listitem"><strong>{running}</strong><span>執行中</span></div>
        <div className="fleet-stat" role="listitem"><strong>{planning} / {executing} / {done}</strong><span>規劃 / 執行 / 完成</span></div>
        <div className={`fleet-stat${alerts > 0 ? " warning" : ""}`} role="listitem"><strong>{alerts}</strong><span>需要關注</span></div>
        <div className="fleet-stat tasks" role="listitem">
          <strong>{doneTasks} / {totalTasks}<em>（{taskPct}%）</em></strong>
          <span>任務完成</span>
          <div className="fleet-progress"><div className="fleet-progress-fill" style={{ width: `${taskPct}%` }} /></div>
        </div>
        <div className="fleet-stat fleet-performance" role="listitem" aria-label="全部 workspace 輪次效能">
          <strong>{fleetMetrics?.sample_count ?? 0} 輪</strong>
          <span>全部 workspace 近 500 輪</span>
          {fleetMetrics && fleetMetrics.sample_count > 0 ? (
            <div className="fleet-performance-grid" title={`${fleetMetrics.workspace_count} 個 workspace 合併後，取時間最新 ${fleetMetrics.limit} 輪`}>
              <span><small>平均</small><b>{formatMetric(fleetMetrics.average_seconds)}</b></span>
              <span><small>P50</small><b>{formatMetric(fleetMetrics.p50_seconds)}</b></span>
              <span><small>P95</small><b>{formatMetric(fleetMetrics.p95_seconds)}</b></span>
              <span><small>最慢</small><b>{formatMetric(fleetMetrics.max_seconds)}</b></span>
              <span className={fleetMetrics.timeout_count ? "warning" : ""}><small>逾時</small><b>{fleetMetrics.timeout_rate_pct}%</b></span>
            </div>
          ) : <small className="fleet-performance-empty">尚無輪次資料</small>}
        </div>
      </div>
      <div className="fleet-filter-row">
        <div className="fleet-filters" role="group" aria-label="Workspace 篩選">
          {filters.map((item) => (
            <button key={item.id} type="button" className={filter === item.id ? "active" : ""}
              aria-pressed={filter === item.id} onClick={() => changeFilter(item.id)}>
              {item.label} <span>{item.count}</span>
            </button>
          ))}
        </div>
        <input className="fleet-search" type="search" aria-label="搜尋 workspace" placeholder="搜尋 workspace…"
          value={search} onChange={(event) => changeSearch(event.target.value)} />
        <span className="muted">顯示 {visibleWorkspaces.length} / {workspaces.length}</span>
      </div>
      <div className="fleet-body">
        <div className="fleet-grid">
          {visibleWorkspaces.map((workspace) => {
            const { done: cardDone, total, pct } = progress(workspace);
            const alert = needsAttention(workspace);
            const unreadIssues = workspace.unread_issues ?? workspace.issues ?? 0;
            const activity = currentActivity(workspace);
            const roundTiming = deriveRoundTiming(workspace, workspace.running, roundNow);
            const metrics = metricsByWorkspace.get(workspace.name);
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
                  {roundTiming && <span className={`round-timer${roundTiming.warning || roundTiming.interrupted ? " warning" : ""}`}>{roundTiming.label}</span>}
                  {(workspace.last_round_seconds ?? 0) > 0 && <span className="muted">⏱ {workspace.last_round_seconds}s</span>}
                </div>
                {total > 0 && workspace.phase !== "plan" && (
                  <div className="fleet-progress" aria-label={`任務 ${cardDone}/${total}`}>
                    <div className="fleet-progress-fill" style={{ width: `${pct}%` }} />
                    <span className="fleet-progress-text">{cardDone}/{total}</span>
                  </div>
                )}
                {activity && <div className="fleet-card-task" title={activity}>{workspace.phase === "exec" ? "→ " : ""}{activity}</div>}
                {metrics && metrics.sample_count > 0 ? (
                  <div className="fleet-card-analysis" aria-label={`近期 ${metrics.sample_count} 輪效能`}>
                    <div className="fleet-card-analysis-head"><strong>近期 {metrics.sample_count} 輪</strong><span>效能</span></div>
                    <div className="fleet-card-analysis-grid">
                      <span><small>平均</small><strong>{formatMetric(metrics.average_seconds)}</strong></span>
                      <span><small>P50</small><strong>{formatMetric(metrics.p50_seconds)}</strong></span>
                      <span><small>P95</small><strong>{formatMetric(metrics.p95_seconds)}</strong></span>
                      <span><small>最慢</small><strong>{formatMetric(metrics.max_seconds)}</strong></span>
                      <span className={metrics.timeout_count ? "warning" : ""}><small>逾時</small><strong>{metrics.timeout_rate_pct}%</strong></span>
                    </div>
                  </div>
                ) : <div className="fleet-card-analysis-empty">尚無輪次效能資料</div>}
                {alert && (
                  <div className="fleet-card-alerts">
                    {workspace.phase !== "done" && (workspace.red_streak ?? 0) > 0 && <span className="chip warning">紅連跳 {workspace.red_streak}</span>}
                    {workspace.phase !== "done" && (workspace.stall_rounds ?? 0) > 0 && <span className="chip subdued">停滯 {workspace.stall_rounds}</span>}
                    {unreadIssues > 0 && <span className="chip issue-chip">issues 未讀 {unreadIssues}</span>}
                    {workspace.phase !== "done" && (workspace.agent_failure_streak ?? 0) > 0 && <span className="chip warning">Agent 異常 {workspace.agent_failure_streak}</span>}
                    {workspace.phase !== "done" && workspace.last_round_timed_out && <span className="chip warning">⏱ 上輪逾時</span>}
                    {workspace.phase !== "done" && (workspace.state_recovery_count ?? 0) > 0 && <span className="chip warning">🛟 state 復原 {workspace.state_recovery_count}</span>}
                    {workspace.state_recovery_pending && <span className="chip warning">🛟 checkpoint</span>}
                    {workspace.goal_changed && <span className="chip warning">goal 已變更</span>}
                    {workspace.stale_loop_pid && <span className="chip warning">⚠ PID 殘留</span>}
                    {workspace.error && <span className="chip warning">❌ state 錯誤</span>}
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
