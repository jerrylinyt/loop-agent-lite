import type { ParallelRunState, TrackEvent, WorkspaceSummary } from "../../shared/api/types";
import { mergeTransactionLabel, parallelPhaseLabel, trackStatusLabel } from "./parallelPhase";

const TRACK_EVENT_LABELS: Record<string, string> = {
  queued: "進入合併佇列",
  dequeued: "離開合併佇列",
  "merge-prepared": "CAS merge prepared",
  merged: "已合併",
  repairing: "回送修復",
  "rollback-prepared": "Rollback prepared",
  "cleanup-evidence-captured": "已保存 track evidence",
  "cleanup-worktree-removed": "已移除 worktree",
  "cleanup-child-removed": "已移除 child workspace",
  cleaned: "清理完成",
  "child-phase": "Child phase",
  "child-state": "Child state",
};

function trackEventDetails(event: TrackEvent): string {
  return [
    event.phase ? `phase ${event.phase}` : "",
    event.merge_stage ? `merge ${event.merge_stage}` : "",
    event.status ? `status ${event.status}` : "",
    Number.isInteger(event.round) ? `round ${event.round}` : "",
    event.recovered ? "recovered" : "",
  ].filter(Boolean).join(" · ");
}

export default function ParallelRunGroup({ workspace, run, readError }: {
  workspace: WorkspaceSummary;
  run?: ParallelRunState;
  readError?: string | null;
}) {
  const tracks = workspace.parallel_tracks ?? [];
  const transaction = workspace.parallel_merge_tx;
  const currentReason = readError || run?.error || run?.stop_reason || workspace.parallel_error || workspace.parallel_stop_reason;
  const lastError = run?.last_error;
  const failed = workspace.parallel_phase === "failed";
  const cannotResume = run?.resumable === false;
  if (workspace.workspace_kind !== "fleet-parent") return null;
  return <section className="parallel-run-group" aria-label="Parallel run tracks">
    {(currentReason || failed || lastError) && <div className="parallel-run-alert" role="alert">
      <strong>{readError ? "Parallel truth 無法讀取" : failed ? "Parallel run 已失敗" : currentReason ? "Parallel run 已停止" : "上次失敗"}</strong>
      {currentReason && <span>{currentReason}</span>}
      {lastError && <span>上次失敗 · {parallelPhaseLabel(lastError.phase)} · {lastError.at} · {lastError.message}</span>}
      {failed && (cannotResume
        ? <span>Coordinator 標示此 run 不可恢復；請保留 evidence，確認後可永久刪除 workspace。</span>
        : <span>{run?.resume_phase ? `可從 ${parallelPhaseLabel(run.resume_phase)} 重試；` : "可由 coordinator 選擇合法恢復點重試；"}按「運行」resume，錯誤紀錄會保留供稽核。</span>)}
    </div>}
    <div className="parallel-run-head">
      <strong>Parallel run</strong>
      <span className="chip subdued">{parallelPhaseLabel(workspace.parallel_phase)}</span>
      <span className="muted">{tracks.filter((track) => ["merged", "cleaned"].includes(track.status)).length}/{tracks.length} tracks</span>
      {!!workspace.parallel_merge_queue?.length && <span className="chip subdued">queue {workspace.parallel_merge_queue.length}</span>}
      {transaction && <span className={`chip merge-transaction stage-${transaction.stage}`} title={`${transaction.expected_sha} → ${transaction.candidate_sha}${transaction.validation_error ? `\n${transaction.validation_error}` : ""}`}>{mergeTransactionLabel(transaction.stage)} · {transaction.track}</span>}
    </div>
    <div className="parallel-track-grid">
      {tracks.map((track) => <article className={`parallel-track status-${track.status}`} key={track.name}>
        <div><strong>{track.name}</strong><span className="phase-badge">{trackStatusLabel(track.status)}</span></div>
        <small>{track.tip ? track.tip.slice(0, 8) : track.pid ? `pid ${track.pid}` : "waiting"}</small>
        {!!track.restart_count && <small>restart {track.restart_count}</small>}
        {!!track.integration_validate_failures && <small className="warning">rollback {track.integration_validate_failures}</small>}
        {track.last_integration_error && <small className="warning track-error-summary" title={track.last_integration_error}>validator: {track.last_integration_error.split("\n").find(Boolean)}</small>}
        {track.evidence_path && <details className="track-evidence"><summary>保留 evidence</summary><div><code>{track.evidence_path}</code>{track.evidence_sha256 && <small>sha256 {track.evidence_sha256}</small>}</div></details>}
        {!!track.status_history?.length && <small className="track-status-history" title={track.status_history.map((entry) => `${entry.at} ${trackStatusLabel(entry.status)}`).join("\n")}>{track.status_history.map((entry) => trackStatusLabel(entry.status)).join(" → ")}</small>}
        {!!track.event_history?.length && <details className="track-event-history"><summary>event history · {track.event_history.length}</summary><ol aria-label={`${track.name} track event history`}>{track.event_history.map((event, index) => <li key={`${event.at}-${event.event}-${index}`}><div><code>{event.event}</code><strong>{TRACK_EVENT_LABELS[event.event] ?? event.event}</strong></div>{trackEventDetails(event) && <small>{trackEventDetails(event)}</small>}<time>{event.at}</time></li>)}</ol></details>}
      </article>)}
      {!tracks.length && <div className="loading-state">規劃收斂後會顯示拆分軌道</div>}
    </div>
    <details className="parallel-history">
      <summary>狀態歷史 · {workspace.parallel_phase_history?.length ?? 0} phases · {workspace.parallel_merge_history?.length ?? 0} merge events</summary>
      <div className="parallel-history-columns">
        <ol aria-label="Parallel phase history">{(workspace.parallel_phase_history ?? []).map((entry, index) => <li key={`${entry.phase}-${entry.started_at}-${index}`}><strong>{parallelPhaseLabel(entry.phase)}</strong><small>{entry.started_at}{entry.duration_seconds !== null && entry.duration_seconds !== undefined ? ` · ${entry.duration_seconds}s` : ""}</small></li>)}</ol>
        <ol aria-label="Merge transaction history">{(workspace.parallel_merge_history ?? []).map((entry, index) => <li key={`${entry.track}-${entry.candidate_sha}-${entry.stage}-${index}`}><strong>{mergeTransactionLabel(entry.stage)} · {entry.track}</strong><small>{entry.at} · {entry.candidate_sha.slice(0, 8)}</small></li>)}</ol>
      </div>
    </details>
  </section>;
}
