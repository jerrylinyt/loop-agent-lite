import { useMemo, useState } from "react";
import type { FleetHistoryEntry, WorkspaceSummary } from "../../shared/api/types";
import Modal from "../../shared/components/Modal";
import { deriveFleetEvents } from "./fleetEvents";

export interface DashboardNotification { id: string; workspace: string; text: string; time: string; severity: "info" | "warning" | "success" }
export function notificationItems(workspaces: WorkspaceSummary[], history: FleetHistoryEntry[]): DashboardNotification[] {
  const items: DashboardNotification[] = deriveFleetEvents(history, 80).map((event) => ({ id: `history:${event.ws}:${event.ts}:${event.text}`, workspace: event.ws, text: event.text, time: event.ts, severity: /完成/.test(event.text) ? "success" : /❌|逾時|RESET|異常/.test(event.text) ? "warning" : "info" }));
  for (const workspace of workspaces) {
    const add = (key: string, text: string) => items.unshift({ id: `state:${workspace.name}:${key}:${workspace.round ?? 0}`, workspace: workspace.name, text, time: `round ${workspace.round ?? 0}`, severity: "warning" });
    if ((workspace.stall_rounds ?? 0) > 0 && workspace.phase !== "done") add("stall", `停滯 ${workspace.stall_rounds} 輪`);
    if ((workspace.state_recovery_count ?? 0) > 0) add("recovery", `State 已復原 ${workspace.state_recovery_count} 次`);
    if (workspace.goal_changed) add("goal", "Goal 已變更");
    if (workspace.error) add("error", `State 錯誤：${workspace.error}`);
  }
  return [...new Map(items.map((item) => [item.id, item])).values()].slice(0, 100);
}
export function readNotificationSeen(): Set<string> {
  try { const value = JSON.parse(localStorage.getItem("dashboard-notification-seen") ?? "[]"); return new Set(Array.isArray(value) ? value.filter((id) => typeof id === "string").slice(-300) : []); }
  catch { return new Set(); }
}

export default function NotificationCenterModal({ workspaces, history, onSelect, onSeenChanged, onClose }: { workspaces: WorkspaceSummary[]; history: FleetHistoryEntry[]; onSelect: (name: string) => void; onSeenChanged: () => void; onClose: () => void }) {
  const items = useMemo(() => notificationItems(workspaces, history), [history, workspaces]);
  const [seen, setSeen] = useState(readNotificationSeen);
  const persist = (next: Set<string>) => { setSeen(next); localStorage.setItem("dashboard-notification-seen", JSON.stringify([...next].slice(-300))); onSeenChanged(); };
  const markAll = () => persist(new Set([...seen, ...items.map((item) => item.id)]));
  return <Modal title="通知中心" description="由目前 state 與有界 history 尾段產生；已讀狀態只存在這個瀏覽器" onClose={onClose} wide footer={<><button type="button" className="secondary-button" disabled={!items.some((item) => !seen.has(item.id))} onClick={markAll}>全部標記已讀</button><span className="muted">{items.filter((item) => !seen.has(item.id)).length} 未讀／{items.length} 則</span></>}>
    <div className="notification-list">
      {items.map((item) => <button type="button" key={item.id} className={`notification-item ${item.severity}${seen.has(item.id) ? " read" : " unread"}`} onClick={() => { persist(new Set([...seen, item.id])); onClose(); onSelect(item.workspace); }}><span><strong>{item.workspace}</strong><small>{item.time}</small></span><p>{item.text}</p></button>)}
      {!items.length && <div className="empty-inline">目前沒有通知</div>}
    </div>
  </Modal>;
}
