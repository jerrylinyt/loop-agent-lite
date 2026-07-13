/** 單一 workspace 的總覽卡片；只負責呈現，不持有 fleet 篩選或批次操作狀態。 */
import type { FleetHistoryEntry, WorkspaceSummary } from "../../shared/api/types";
import { deriveRoundTiming } from "./roundTiming";
import {
  currentActivity, formatMetric, taskProgress, workspaceNeedsAttention,
} from "./fleetViewModel";
import { parallelNeedsAttention, parallelPhaseLabel, parallelVisualPhase } from "./parallelPhase";

const PHASE_NAMES: Record<string, string> = { plan: "規劃期", exec: "執行期", done: "🏁 完成" };

export default function FleetWorkspaceCard({ workspace, metrics, roundNow, onSelect, disabled = false }: {
  workspace: WorkspaceSummary;
  metrics?: FleetHistoryEntry["metrics"];
  roundNow: number;
  onSelect: (name: string) => void;
  disabled?: boolean;
}) {
  const { done, total, pct } = taskProgress(workspace);
  const alert = workspaceNeedsAttention(workspace);
  const unreadIssues = workspace.unread_issues ?? workspace.issues ?? 0;
  const activity = currentActivity(workspace);
  const roundTiming = deriveRoundTiming(workspace, workspace.running, roundNow);
  const isParallel = workspace.workspace_kind === "fleet-parent";
  const completed = workspace.phase === "done" || workspace.parallel_phase === "done";
  const parallelAttention = isParallel && parallelNeedsAttention(workspace.parallel_phase, workspace.parallel_tracks);
  const phaseLabel = isParallel ? parallelPhaseLabel(workspace.parallel_phase) : PHASE_NAMES[workspace.phase ?? ""] ?? "—";
  const visualPhase = isParallel
    ? parallelVisualPhase(workspace.parallel_phase, workspace.parallel_tracks, workspace.running)
    : workspace.phase ?? "unknown";

  return (
    <button type="button" disabled={disabled} className={`fleet-card phase-${visualPhase}${workspace.running ? " running" : ""}`} onClick={() => onSelect(workspace.name)}>
      <div className="fleet-card-head">
        <strong>{workspace.name}</strong>
        {workspace.running && <span className="breathing-dot" aria-label="執行中" />}
      </div>
      <div className="fleet-card-meta">
        <span className={`phase-badge phase-${visualPhase}`}>{phaseLabel}</span>
        <span className="muted">round {workspace.round ?? 0}</span>
        {!isParallel && workspace.phase === "plan" && <span className="muted">flag {workspace.flag ?? 0}</span>}
        {!isParallel && workspace.phase === "exec" && <span className="muted">done {workspace.done_count ?? 0}</span>}
        {roundTiming && <span className={`round-timer${roundTiming.warning || roundTiming.interrupted ? " warning" : ""}`}>{roundTiming.label}</span>}
        {(workspace.last_round_seconds ?? 0) > 0 && <span className="muted">⏱ {workspace.last_round_seconds}s</span>}
      </div>
      {total > 0 && (isParallel || workspace.phase !== "plan") && (
        <div className="fleet-progress" aria-label={`任務 ${done}/${total}`}>
          <div className="fleet-progress-fill" style={{ width: `${pct}%` }} />
          <span className="fleet-progress-text">{done}/{total}</span>
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
          <div className="fleet-card-anomaly-grid" title="有 Git 變更但未回報仍算異常；人工中斷輪不計">
            <span className={metrics.missing_done_count ? "warning" : ""}><small>未回 DONE</small><strong>{metrics.missing_done_count} 次</strong></span>
            <span className={metrics.missing_done_count ? "warning" : ""}><small>異常率</small><strong>{metrics.missing_done_rate_pct}%</strong></span>
          </div>
        </div>
      ) : <div className="fleet-card-analysis-empty">尚無輪次效能資料</div>}
      {alert && (
        <div className="fleet-card-alerts">
          {parallelAttention && <span className="chip warning">{workspace.parallel_phase === "failed" ? "Parallel 執行失敗" : "Track 修復／失敗"}</span>}
          {!completed && (workspace.red_streak ?? 0) > 0 && <span className="chip warning">紅連跳 {workspace.red_streak}</span>}
          {!completed && (workspace.stall_rounds ?? 0) > 0 && <span className="chip subdued">停滯 {workspace.stall_rounds}</span>}
          {unreadIssues > 0 && <span className="chip issue-chip">issues 未讀 {unreadIssues}</span>}
          {!completed && (workspace.agent_failure_streak ?? 0) > 0 && <span className="chip warning">Agent 異常 {workspace.agent_failure_streak}</span>}
          {!completed && workspace.last_round_timed_out && <span className="chip warning">⏱ 上輪逾時</span>}
          {!completed && (workspace.state_recovery_count ?? 0) > 0 && <span className="chip warning">🛟 state 復原 {workspace.state_recovery_count}</span>}
          {workspace.state_recovery_pending && <span className="chip warning">🛟 checkpoint</span>}
          {workspace.goal_changed && <span className="chip warning">goal 已變更</span>}
          {workspace.stale_loop_pid && <span className="chip warning">⚠ PID 殘留</span>}
          {workspace.error && <span className="chip warning">❌ state 錯誤</span>}
        </div>
      )}
      {workspace.repo && <div className="fleet-card-repo" title={workspace.repo}>{workspace.repo}</div>}
    </button>
  );
}
