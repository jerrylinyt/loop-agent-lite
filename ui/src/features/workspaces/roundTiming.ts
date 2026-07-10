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
  if (!value) return null;
  const timestamp = Date.parse(value);
  return Number.isFinite(timestamp) ? timestamp : null;
}

export function formatRoundClock(totalSeconds: number): string {
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
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!ticking) return;
    setNow(Date.now());
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [ticking]);
  return now;
}
