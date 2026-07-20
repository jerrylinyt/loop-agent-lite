/** Ralph runner 的純資料模型與 progress.txt 增量輪詢 hook；與 RalphView 呈現分離。 */
import { useEffect, useRef, useState } from "react";
import { getRalphProgress } from "../../shared/api/client";
import type { RalphExitReason } from "../../shared/api/types";

export type RalphTone = "success" | "warning" | "danger" | "muted";

/** ralph.sh 終態原因 → 顯示標籤與色調（RALPH_CONTRACT §A）。 */
export const RALPH_EXIT: Record<RalphExitReason, { label: string; tone: RalphTone }> = {
  completed: { label: "已完成", tone: "success" },
  iterations_exhausted: { label: "迭代耗盡", tone: "warning" },
  failed: { label: "失敗", tone: "danger" },
  interrupted: { label: "已中斷", tone: "muted" },
  usage_limit_giveup: { label: "用量上限放棄", tone: "danger" },
};

/** args_style 內建模板（RALPH_CONTRACT「args_style → template」＋ §H ARGS_STYLES）。 */
export const RALPH_ARGS_TEMPLATES: Record<"positional" | "snarktank", string[]> = {
  positional: ["{iterations}", "{tool}", "{model}"],
  snarktank: ["--tool", "{tool}", "{iterations}"],
};

/** stories 完成比例（0..100）；total 為 0 時回 0。 */
export function storyProgressPct(done?: number, total?: number): number {
  const totalValue = total ?? 0;
  const doneValue = done ?? 0;
  return totalValue > 0 ? Math.round((doneValue / totalValue) * 100) : 0;
}

/** active_model 與設定模型不同即視為已降級（RALPH_CONTRACT §I）。 */
export function isModelDowngraded(activeModel?: string, configModel?: string): boolean {
  return !!activeModel && !!configModel && activeModel !== configModel;
}

/** ISO 時間 → 本機 HH:MM；無法解析時原樣回傳，空值回空字串。 */
export function formatResumeAt(value?: string | null): string {
  if (!value) return "";
  const timestamp = Date.parse(value);
  if (!Number.isFinite(timestamp)) return value;
  return new Date(timestamp).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

/** 距離 resume_at 的剩餘秒數（不為負）；無法解析回 null 讓呼叫端不顯示倒數。 */
export function resumeCountdownSeconds(resumeAt: string | null | undefined, nowMs: number): number | null {
  if (!resumeAt) return null;
  const timestamp = Date.parse(resumeAt);
  if (!Number.isFinite(timestamp)) return null;
  return Math.max(0, Math.ceil((timestamp - nowMs) / 1000));
}

/**
 * progress.txt 增量輪詢：首抓以 offset<0 取尾段，之後以回傳 size 當下一個 offset 續讀。
 * 執行中輪詢較密、停止後放慢；沿用 read_incremental 的 size/offset/truncated 契約。
 */
export function useRalphProgress(workspaceName: string, running: boolean): string {
  const [text, setText] = useState("");
  const offsetRef = useRef(-1);

  useEffect(() => {
    // 切換 workspace 必須重讀尾段，避免沿用上一個 workspace 的 offset 造成錯位。
    offsetRef.current = -1;
    setText("");
  }, [workspaceName]);

  useEffect(() => {
    if (!workspaceName) return;
    let active = true;
    let timer = 0;
    const tick = async () => {
      const response = await getRalphProgress(workspaceName, offsetRef.current);
      if (!active) return;
      if (response && typeof response.size === "number" && !response.error) {
        const previousOffset = offsetRef.current;
        const initial = previousOffset < 0;
        offsetRef.current = response.size;
        if (response.data) {
          // 首抓、截斷（truncated）或檔案輪替（size 回退到舊 offset 之前）時整段取代，其餘 append-only 累加。
          const replace = initial || !!response.truncated || response.size < previousOffset;
          setText((prev) => (replace ? response.data : prev + response.data));
        } else if (initial) {
          setText("");
        }
      }
      if (active) timer = window.setTimeout(() => void tick(), running ? 2000 : 10000);
    };
    void tick();
    return () => {
      active = false;
      window.clearTimeout(timer);
    };
  }, [workspaceName, running]);

  return text;
}
