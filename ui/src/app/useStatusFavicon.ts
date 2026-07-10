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
  running: "🟢",
  warning: "🔴",
  done: "🏁",
  idle: "⚪"
};

function deriveStatus(workspace: WorkspaceSummary | undefined, state: WorkspaceState | null): Status {
  if (!workspace || !state || state.error) return "none";
  if (state.phase === "done") return "done";
  if (workspace.running && state.red_streak > 0) return "warning";
  if (workspace.running) return "running";
  return "idle";
}

function drawFavicon(status: Exclude<Status, "none"> | "plain"): string {
  const canvas = document.createElement("canvas");
  canvas.width = canvas.height = 32;
  const ctx = canvas.getContext("2d");
  if (!ctx) return "";
  ctx.fillStyle = "#161b22";
  ctx.beginPath();
  ctx.roundRect(0, 0, 32, 32, 7);
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
