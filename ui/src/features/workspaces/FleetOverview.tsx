import type { WorkspaceSummary } from "../../shared/api/types";

const PHASE_NAMES: Record<string, string> = { plan: "規劃期", exec: "執行期", done: "🏁 完成" };

function progress(workspace: WorkspaceSummary): { done: number; total: number; pct: number } {
  const total = workspace.plan_len ?? 0;
  const done = workspace.completed ?? 0;
  return { done, total, pct: total > 0 ? Math.round((done / total) * 100) : 0 };
}

/** 監控電視牆:聚合統計 + 全 fleet 即時卡片,資料走既有 SSE workspaces 事件。 */
export default function FleetOverview({ workspaces, onSelect }: {
  workspaces: WorkspaceSummary[];
  onSelect: (name: string) => void;
}) {
  const running = workspaces.filter((workspace) => workspace.running).length;
  const done = workspaces.filter((workspace) => workspace.phase === "done").length;
  const planning = workspaces.filter((workspace) => workspace.phase === "plan").length;
  const executing = workspaces.filter((workspace) => workspace.phase === "exec").length;
  const totalTasks = workspaces.reduce((sum, workspace) => sum + (workspace.plan_len ?? 0), 0);
  const doneTasks = workspaces.reduce((sum, workspace) => sum + (workspace.completed ?? 0), 0);
  const alerts = workspaces.filter((workspace) => (workspace.red_streak ?? 0) > 0 || (workspace.issues ?? 0) > 0).length;
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
      <div className="fleet-grid">
        {workspaces.map((workspace) => {
          const { done: cardDone, total, pct } = progress(workspace);
          const alert = (workspace.red_streak ?? 0) > 0 || (workspace.stall_rounds ?? 0) > 0 || (workspace.issues ?? 0) > 0;
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
              {alert && (
                <div className="fleet-card-alerts">
                  {(workspace.red_streak ?? 0) > 0 && <span className="chip warning">紅連跳 {workspace.red_streak}</span>}
                  {(workspace.stall_rounds ?? 0) > 0 && <span className="chip subdued">停滯 {workspace.stall_rounds}</span>}
                  {(workspace.issues ?? 0) > 0 && <span className="chip issue-chip">issues {workspace.issues}</span>}
                </div>
              )}
              {workspace.repo && <div className="fleet-card-repo" title={workspace.repo}>{workspace.repo}</div>}
            </button>
          );
        })}
        {!workspaces.length && <div className="empty-inline">尚未建立 workspace</div>}
      </div>
    </main>
  );
}
