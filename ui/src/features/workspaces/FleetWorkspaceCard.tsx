/** 單一 workspace 的總覽卡片；只負責呈現，不持有 fleet 篩選或批次操作狀態。 */
import type { FleetHistoryEntry, WorkspaceSummary } from "../../shared/api/types";
import { deriveRoundTiming } from "./roundTiming";
import { RALPH_EXIT, storyProgressPct } from "./ralphViewModel";
import {
  currentActivity, formatMetric, taskProgress, workspaceNeedsAttention,
} from "./fleetViewModel";

const PHASE_NAMES: Record<string, string> = { plan: "規劃期", exec: "執行期", done: "完成" };

export default function FleetWorkspaceCard({ workspace, metrics, roundNow, onSelect }: {
  workspace: WorkspaceSummary;
  metrics?: FleetHistoryEntry["metrics"];
  roundNow: number;
  onSelect: (name: string) => void;
}) {
  if (workspace.runner === "ralph") {
    const ralph = workspace.ralph ?? {};
    const storiesDone = ralph.stories_done ?? 0;
    const storiesTotal = ralph.stories_total ?? 0;
    const exit = ralph.exit_reason ? RALPH_EXIT[ralph.exit_reason] : null;
    return (
      <button type="button" className={`fleet-card fleet-card-ralph${workspace.running ? " running" : ""}`} onClick={() => onSelect(workspace.name)}>
        <div className="fleet-card-head">
          <strong>{workspace.name}</strong>
          {workspace.running && <span className="breathing-dot" aria-label="執行中" />}
        </div>
        <div className="fleet-card-meta">
          <span className="phase-badge ralph-runner-tag">Ralph</span>
          <span className="muted">迭代 {ralph.iteration ?? 0}/{ralph.max_iterations ?? "?"}</span>
          {ralph.usage_limit && <span className="chip warning" title="Agent 用量上限，監督層等待重啟中">⏳ 用量上限</span>}
          {exit && <span className={`chip ralph-exit-${exit.tone}`}>{exit.label}</span>}
          {ralph.stalled && !exit && <span className="chip warning">停滯</span>}
          {ralph.sentinel_complete && !exit && <span className="muted">已見完成訊號</span>}
        </div>
        {storiesTotal > 0 && (
          <div className="fleet-progress" aria-label={`Stories ${storiesDone}/${storiesTotal}`}>
            <div className="fleet-progress-fill" style={{ width: `${storyProgressPct(storiesDone, storiesTotal)}%` }} />
            <span className="fleet-progress-text">Stories {storiesDone}/{storiesTotal}</span>
          </div>
        )}
        {workspace.error && <div className="fleet-card-alerts"><span className="chip warning">錯誤：state 錯誤</span></div>}
        {workspace.repo && <div className="fleet-card-repo" title={workspace.repo}>{workspace.repo}</div>}
      </button>
    );
  }

  const { done, total, pct } = taskProgress(workspace);
  const alert = workspaceNeedsAttention(workspace);
  const unreadIssues = workspace.unread_issues ?? workspace.issues ?? 0;
  const activity = currentActivity(workspace);
  const roundTiming = deriveRoundTiming(workspace, workspace.running, roundNow);

  return (
    <button type="button" className={`fleet-card phase-${workspace.phase ?? "unknown"}${workspace.running ? " running" : ""}`} onClick={() => onSelect(workspace.name)}>
      <div className="fleet-card-head">
        <strong>{workspace.name}</strong>
        {workspace.running && <span className="breathing-dot" aria-label="執行中" />}
      </div>
      <div className="fleet-card-meta">
        {workspace.runner === "parallel-supervisor" && <span className="phase-badge parallel-runner-tag">Parallel</span>}
        <span className={`phase-badge phase-${workspace.phase ?? "unknown"}`}>{PHASE_NAMES[workspace.phase ?? ""] ?? "—"}</span>
        {workspace.runner === "parallel-supervisor" && <span className="muted">{workspace.parallel?.status ?? "unknown"} · batch {workspace.parallel?.batch ?? "—"}</span>}
        <span className="muted">round {workspace.round ?? 0}</span>
        {workspace.phase === "plan" && <span className="muted">flag {workspace.flag ?? 0}</span>}
        {workspace.phase === "exec" && <span className="muted">done {workspace.done_count ?? 0}</span>}
        {roundTiming && <span className={`round-timer${roundTiming.warning || roundTiming.interrupted ? " warning" : ""}`}>{roundTiming.label}</span>}
        {(workspace.last_round_seconds ?? 0) > 0 && <span className="muted">上輪 {workspace.last_round_seconds}s</span>}
      </div>
      {total > 0 && workspace.phase !== "plan" && (
        <div className="fleet-progress" aria-label={`任務 ${done}/${total}`}>
          <div className="fleet-progress-fill" style={{ width: `${pct}%` }} />
          <span className="fleet-progress-text">{done}/{total}</span>
        </div>
      )}
      {activity && <div className="fleet-card-task" title={activity}>{workspace.phase === "exec" ? "目前：" : ""}{activity}</div>}
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
          {workspace.phase !== "done" && (workspace.red_streak ?? 0) > 0 && <span className="chip warning">紅連跳 {workspace.red_streak}</span>}
          {workspace.phase !== "done" && (workspace.stall_rounds ?? 0) > 0 && <span className="chip subdued">停滯 {workspace.stall_rounds}</span>}
          {unreadIssues > 0 && <span className="chip issue-chip">issues 未讀 {unreadIssues}</span>}
          {workspace.phase !== "done" && (workspace.agent_failure_streak ?? 0) > 0 && <span className="chip warning">Agent 異常 {workspace.agent_failure_streak}</span>}
          {workspace.phase !== "done" && workspace.last_round_timed_out && <span className="chip warning">上輪逾時</span>}
          {workspace.phase !== "done" && (workspace.state_recovery_count ?? 0) > 0 && <span className="chip warning">state 復原 {workspace.state_recovery_count}</span>}
          {workspace.state_recovery_pending && <span className="chip warning">checkpoint</span>}
          {workspace.goal_changed && <span className="chip warning">goal 已變更</span>}
          {workspace.stale_loop_pid && <span className="chip warning">警告：PID 殘留</span>}
          {workspace.error && <span className="chip warning">錯誤：state 錯誤</span>}
          {workspace.parallel?.status === "blocked" && <span className="chip warning">Parallel blocked</span>}
          {workspace.parallel?.error && <span className="chip warning" title={workspace.parallel.error}>Parallel：{workspace.parallel.error}</span>}
        </div>
      )}
      {workspace.repo && <div className="fleet-card-repo" title={workspace.repo}>{workspace.repo}</div>}
    </button>
  );
}
