/** 依 state 的開始、deadline 與中斷時間計算顯示計時；瀏覽器只更新畫面，不製造高頻 SSE。 */
import { useEffect, useState } from "react";

export interface RoundTimingFields {
  round?: number;
  round_started_at?: string | null;
  round_deadline_at?: string | null;
  round_interrupted_at?: string | null;
}

export interface DerivedRoundTiming {
  elapsedSeconds: number;
  remainingSeconds: number | null;
  interrupted: boolean;
  uncertainEnd: boolean;
  warning: boolean;
  label: string;
}

function parseTimestamp(value?: string | null): number | null {
  // Date.parse 對無效/舊資料會回 NaN；統一轉成 null 讓呼叫端選擇不顯示。
  if (!value) return null;
  const timestamp = Date.parse(value);
  return Number.isFinite(timestamp) ? timestamp : null;
}

export function formatRoundClock(totalSeconds: number): string {
  /** 將秒數固定格式成 mm:ss 或 h:mm:ss，負數一律視為 0。 */
  const seconds = Math.max(0, Math.floor(totalSeconds));
  const hours = Math.floor(seconds / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  const remainder = seconds % 60;
  return hours > 0
    ? `${hours}:${String(minutes).padStart(2, "0")}:${String(remainder).padStart(2, "0")}`
    : `${minutes}:${String(remainder).padStart(2, "0")}`;
}

export function deriveRoundTiming(
  fields: RoundTimingFields,
  running: boolean,
  nowMs: number
): DerivedRoundTiming | null {
  // 根據 running/interrupted/deadline 判斷計時基準與最後 60 秒警示，不自行修正 state。
  const started = parseTimestamp(fields.round_started_at);
  if (started === null) return null;
  const interruptedAt = parseTimestamp(fields.round_interrupted_at);
  const end = interruptedAt ?? nowMs;
  const elapsedSeconds = Math.max(0, Math.floor((end - started) / 1000));
  const deadline = parseTimestamp(fields.round_deadline_at);
  const remainingSeconds = deadline === null ? null : Math.ceil((deadline - end) / 1000);
  const interrupted = !running;
  const uncertainEnd = interrupted && interruptedAt === null;
  const warning = !interrupted && remainingSeconds !== null && remainingSeconds <= 60;
  let label: string;
  if (interrupted) {
    label = `⏸ round ${fields.round ?? "?"} 中斷 · ${uncertainEnd ? "至少 " : ""}${formatRoundClock(elapsedSeconds)}`;
  } else {
    const deadlineLabel = remainingSeconds === null
      ? "無 timeout"
      : remainingSeconds < 0
        ? `已超時 ${formatRoundClock(-remainingSeconds)}`
        : `剩 ${formatRoundClock(remainingSeconds)}`;
    label = `⏱ 本輪 ${formatRoundClock(elapsedSeconds)} · ${deadlineLabel}`;
  }
  return { elapsedSeconds, remainingSeconds, interrupted, uncertainEnd, warning, label };
}

export function useRoundNow(ticking: boolean): number {
  /** 只有畫面存在進行中 round 時才啟動每秒 timer，避免無意義重繪。 */
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!ticking) return;
    setNow(Date.now());
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [ticking]);
  return now;
}
