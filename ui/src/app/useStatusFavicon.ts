/** 將目前 workspace 狀態投影到瀏覽器標題與 favicon；只改 UI，不回寫 coordinator state。 */
import { useEffect, useRef } from "react";
import type { WorkspaceState, WorkspaceSummary } from "../shared/api/types";

type Status = "running" | "warning" | "done" | "idle" | "none";

const DOT_COLORS: Record<Exclude<Status, "none">, string> = {
  running: "#2ea043",
  warning: "#f85149",
  done: "#58a6ff",
  idle: "#8b949e"
};

const TITLE_MARKS: Record<Exclude<Status, "none">, string> = {
  running: "執行中",
  warning: "警告",
  done: "完成",
  idle: "已停止"
};

function deriveStatus(workspace: WorkspaceSummary | undefined, state: WorkspaceState | null): Status {
  // 優先序反映操作重要性：完成 > 執行中紅燈 > 執行中 > 停止；無 workspace 不覆蓋預設圖示。
  if (!workspace || !state || state.error) return "none";
  if (state.phase === "done") return "done";
  if (workspace.running && state.red_streak > 0) return "warning";
  if (workspace.running) return "running";
  return "idle";
}

function drawFavicon(status: Exclude<Status, "none"> | "plain"): string {
  // 以 canvas 在本機產生小圖，不依賴外部圖片；狀態圓點與標題 emoji 互相補充。
  const canvas = document.createElement("canvas");
  canvas.width = canvas.height = 32;
  const ctx = canvas.getContext("2d");
  if (!ctx) return "";
  ctx.fillStyle = "#161b22";
  ctx.beginPath();
  if (typeof ctx.roundRect === "function") {
    ctx.roundRect(0, 0, 32, 32, 7);
  } else {
    ctx.rect(0, 0, 32, 32); // 舊 Safari/Chromium 沒有 roundRect;方角退化,不能讓 effect 拋錯打掛整個 app
  }
  ctx.fill();
  ctx.fillStyle = "#e6edf3";
  ctx.font = "700 18px system-ui, sans-serif";
  ctx.textAlign = "center";
  ctx.textBaseline = "middle";
  ctx.fillText("⌁", 16, 16);
  if (status !== "plain") {
    ctx.fillStyle = DOT_COLORS[status];
    ctx.beginPath();
    ctx.arc(23, 23, 7, 0, Math.PI * 2);
    ctx.fill();
    ctx.strokeStyle = "#0d1117";
    ctx.lineWidth = 2;
    ctx.stroke();
  }
  return canvas.toDataURL("image/png");
}

function setFavicon(href: string) {
  // 重用既有 link，沒有才建立；避免每次 SSE 更新累積多個 favicon 節點。
  if (!href) return;
  let link = document.querySelector<HTMLLinkElement>('link[rel="icon"]');
  if (!link) {
    link = document.createElement("link");
    link.rel = "icon";
    document.head.appendChild(link);
  }
  link.type = "image/png";
  link.href = href;
}

/** tab 標題與 favicon 隨選中 workspace 的即時狀態變化;掛在背景 tab 也能一眼看出死活。 */
export default function useStatusFavicon(
  workspace: WorkspaceSummary | undefined,
  state: WorkspaceState | null,
  selected: string
) {
  const lastDrawn = useRef("");

  useEffect(() => {
    const status = deriveStatus(workspace, state);
    if (status === "none") {
      document.title = selected ? `loop-lite · ${selected}` : "loop-lite";
      if (lastDrawn.current !== "plain") {
        setFavicon(drawFavicon("plain"));
        lastDrawn.current = "plain";
      }
      return;
    }
    document.title = `${TITLE_MARKS[status]} ${selected} · r${state?.round ?? 0} · loop-lite`;
    if (lastDrawn.current !== status) {
      setFavicon(drawFavicon(status));
      lastDrawn.current = status;
    }
  }, [workspace, state, selected]);
}
