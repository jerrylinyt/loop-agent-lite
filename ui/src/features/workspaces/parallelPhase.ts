const PARALLEL_PHASE_LABELS: Record<string, string> = {
  planning: "規劃中",
  "awaiting-approval": "等待核准",
  splitting: "建立軌道中",
  exec: "平行執行中",
  executing: "平行執行中",
  merging: "整合中",
  final: "最終驗收中",
  cleaning: "清理中",
  stopping: "停止中",
  stopped: "已停止",
  failed: "執行失敗",
  done: "🏁 完成",
};

const TRACK_STATUS_LABELS: Record<string, string> = {
  pending: "等待中",
  running: "執行中",
  "merge-ready": "待合併",
  merging: "整合中",
  repairing: "修復中",
  merged: "已合併",
  failed: "失敗",
  stopped: "已停止",
  cleaned: "已清理",
};

export function parallelPhaseLabel(phase?: string | null): string {
  if (!phase) return PARALLEL_PHASE_LABELS.planning;
  return PARALLEL_PHASE_LABELS[phase] ?? phase;
}

export function trackStatusLabel(status?: string | null): string {
  if (!status) return TRACK_STATUS_LABELS.pending;
  return TRACK_STATUS_LABELS[status] ?? status;
}

export function parallelNeedsAttention(phase?: string | null, tracks: TrackState[] = []): boolean {
  return phase === "failed" || tracks.some((track) => ["repairing", "failed"].includes(track.status));
}

export function parallelMutationBlocked(readError?: string | null, resumable?: boolean): boolean {
  // failed/error 本身不是不可恢復的同義詞；只有 truth 無法讀或 coordinator 明確拒絕 resume 才封鎖。
  return !!readError || resumable === false;
}

/** 將 coordinator 的細 phase 收斂成既有 badge/card 的四種顏色語意。 */
export function parallelVisualPhase(
  phase?: string | null,
  tracks: TrackState[] = [],
  running = false
): "plan" | "exec" | "done" | "failed" {
  if (phase === "done") return "done";
  if (parallelNeedsAttention(phase, tracks)) return "failed";
  if (running || ["splitting", "exec", "executing", "merging", "final", "cleaning", "stopping"].includes(phase ?? "")) return "exec";
  return "plan";
}

const MERGE_TRANSACTION_LABELS: Record<string, string> = {
  prepared: "CAS 準備",
  "ref-updated": "CAS 已更新 ref",
  validating: "Integration 驗證中",
  validated: "Integration 驗證通過",
  "rollback-prepared": "Rollback 準備",
  "rolled-back": "Rollback 已完成",
};

export function mergeTransactionLabel(stage?: string | null): string {
  if (!stage) return "";
  return MERGE_TRANSACTION_LABELS[stage] ?? stage;
}
import type { TrackState } from "../../shared/api/types";
