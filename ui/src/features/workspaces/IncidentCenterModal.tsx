import { useMemo } from "react";
import type { FleetHistoryEntry, WorkspaceSummary } from "../../shared/api/types";
import Modal from "../../shared/components/Modal";
import { parseHistory } from "./historyParser";
import { workspaceDiagnostics } from "./workspaceDiagnostics";

interface Signal { ws: string; ts: string; round: number; text: string }
interface Incident { id: string; ws: string; start: string; end: string; rounds: number[]; signals: string[] }
function groupIncidents(history: FleetHistoryEntry[]): Incident[] {
  const signals: Signal[] = [];
  for (const entry of history) for (const row of parseHistory(entry.data).rows) {
    if (row.validate === "FAIL") signals.push({ ws: entry.name, ts: row.ts, round: row.round, text: "Validate 失敗" });
    if (row.timedOut) signals.push({ ws: entry.name, ts: row.ts, round: row.round, text: "Agent 逾時" });
    if (row.missingDone) signals.push({ ws: entry.name, ts: row.ts, round: row.round, text: "未回 phase DONE" });
    if (/RESET|紅燈|異常/i.test(row.event)) signals.push({ ws: entry.name, ts: row.ts, round: row.round, text: row.event });
  }
  signals.sort((a, b) => a.ws.localeCompare(b.ws) || a.ts.localeCompare(b.ts));
  const incidents: Incident[] = [];
  for (const signal of signals) {
    const previous = incidents[incidents.length - 1];
    const distance = previous && previous.ws === signal.ws ? new Date(signal.ts).getTime() - new Date(previous.end).getTime() : Infinity;
    if (previous && distance >= 0 && distance <= 5 * 60_000) {
      previous.end = signal.ts;
      if (!previous.rounds.includes(signal.round)) previous.rounds.push(signal.round);
      if (!previous.signals.includes(signal.text)) previous.signals.push(signal.text);
    } else incidents.push({ id: `${signal.ws}-${signal.ts}`, ws: signal.ws, start: signal.ts, end: signal.ts, rounds: [signal.round], signals: [signal.text] });
  }
  return incidents.reverse();
}

export default function IncidentCenterModal({ workspaces, history, onSelect, onClose }: { workspaces: WorkspaceSummary[]; history: FleetHistoryEntry[]; onSelect: (name: string) => void; onClose: () => void }) {
  const historical = useMemo(() => groupIncidents(history), [history]);
  const active = workspaces.map((workspace) => ({ workspace, diagnostics: workspaceDiagnostics(workspace) })).filter((entry) => entry.diagnostics.length > 0);
  return <Modal title="Incident 中心" description="同 workspace 五分鐘內的異常訊號會聚為一次事件；現行問題與歷史事件分開呈現" onClose={onClose} extraWide>
    <h3 className="section-heading">現行事件</h3>
    <div className="incident-list">
      {active.map(({ workspace, diagnostics }) => <button type="button" className="incident-card active" key={workspace.name} onClick={() => { onClose(); onSelect(workspace.name); }}><span><strong>{workspace.name}</strong><small>目前 · round {workspace.round ?? 0}</small></span><div>{diagnostics.map((item) => <span className="chip warning" key={item.id}>{item.title}</span>)}</div><p>可能根因：{diagnostics[0]?.evidence}</p></button>)}
      {!active.length && <div className="empty-inline">目前沒有現行 incident</div>}
    </div>
    <h3 className="section-heading">歷史關聯事件</h3>
    <div className="incident-list">
      {historical.slice(0, 50).map((incident) => <button type="button" className="incident-card" key={incident.id} onClick={() => { onClose(); onSelect(incident.ws); }}><span><strong>{incident.ws}</strong><small>{incident.start} · round {incident.rounds.join(", ")}</small></span><div>{incident.signals.map((signal) => <span className="chip subdued" key={signal}>{signal}</span>)}</div><p>關聯依據：同一 workspace 且訊號間隔不超過五分鐘；這是時間相關，不宣稱已證明因果。</p></button>)}
      {!historical.length && <div className="empty-inline">history 尚無可關聯的異常訊號</div>}
    </div>
  </Modal>;
}
