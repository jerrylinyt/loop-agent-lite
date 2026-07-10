import { useEffect, useState } from "react";
import { getJson } from "../../shared/api/client";
import type { IncrementalResponse } from "../../shared/api/types";
import { parseHistory, type HistoryRow } from "./historyParser";

const MAX_BARS = 60;
const BAR_STEP = 5;
const HEIGHT = 16;

function barClass(row: HistoryRow): string {
  if (row.event.includes("RESET")) return "spark-reset";
  if (row.tamper) return "spark-fail";
  if (row.phaseRaw === "plan") return "spark-plan";
  if (row.validate === "PASS") return "spark-pass";
  if (row.validate === "FAIL") return "spark-fail";
  return "spark-plan";
}

/** 最近 N 輪的迷你趨勢帶:綠=驗證綠、紅=驗證紅/竄改、灰=規劃、橙=reset;點擊開輪次紀錄。 */
export default function RoundSparkline({ workspace, round, onOpen }: {
  workspace: string;
  round: number;
  onOpen: () => void;
}) {
  const [rows, setRows] = useState<HistoryRow[]>([]);

  useEffect(() => {
    let active = true;
    void (async () => {
      const response = await getJson<IncrementalResponse>(`/api/history?ws=${encodeURIComponent(workspace)}&offset=-1`);
      if (!active || !response) return;
      // parseHistory 回 newest-first;取最近 N 輪後翻回「舊在左、新在右」
      setRows(parseHistory(response.data).rows.slice(0, MAX_BARS).reverse());
    })();
    return () => { active = false; };
  }, [workspace, round]);

  if (!rows.length) return null;
  const width = rows.length * BAR_STEP;
  return (
    <button type="button" className="round-sparkline" aria-label="輪次趨勢（點擊看逐輪判定）" title="輪次趨勢（點擊看逐輪判定）" onClick={onOpen}>
      <svg width={width} height={HEIGHT} viewBox={`0 0 ${width} ${HEIGHT}`} aria-hidden="true">
        {rows.map((row, index) => {
          const barHeight = row.phaseRaw === "plan" ? 8 : 12;
          return (
            <rect key={`${row.round}-${index}`} className={barClass(row)} x={index * BAR_STEP} y={HEIGHT - barHeight} width={BAR_STEP - 1} height={barHeight} rx={1}>
              <title>{`r${row.round} ${row.phase}${row.task ? ` ${row.task}` : ""}${row.validate !== "-" ? ` ${row.validate}` : ""}${row.event ? ` · ${row.event}` : ""}`}</title>
            </rect>
          );
        })}
      </svg>
    </button>
  );
}
