import { useCallback, useEffect, useState } from "react";
import { getJson } from "../../shared/api/client";
import Modal from "../../shared/components/Modal";
import type { IncrementalResponse } from "../../shared/api/types";
import { parseHistory, type HistoryRow } from "./historyParser";

export default function HistoryModal({ workspace, onClose }: { workspace: string; onClose: () => void }) {
  const [rows, setRows] = useState<HistoryRow[]>([]);
  const [note, setNote] = useState("");
  const [loading, setLoading] = useState(true);

  const load = useCallback(async () => {
    setLoading(true);
    const response = await getJson<IncrementalResponse>(`/api/history?ws=${encodeURIComponent(workspace)}&offset=-1`);
    setLoading(false);
    if (!response) {
      setNote("❌ 讀取 history.log 失敗");
      return;
    }
    const { rows: parsed, unparsed } = parseHistory(response.data);
    setRows(parsed);
    const notes: string[] = [];
    if (response.size - response.data.length > 0) notes.push("檔案較大，僅顯示最近的紀錄");
    if (unparsed > 0) notes.push(`${unparsed} 行無法解析（舊格式）`);
    setNote(notes.join("；"));
  }, [workspace]);

  useEffect(() => { void load(); }, [load]);

  return (
    <Modal title="輪次紀錄" description="每一輪輪末的 coordinator 判定（history.log 投影，唯讀）" onClose={onClose} wide footer={
      <><button type="button" className="secondary-button" onClick={() => void load()} disabled={loading}>{loading ? "載入中…" : "重新整理"}</button><span role="status" className="muted">{note}</span></>
    }>
      <div className="modal-table-scroll">
        <table>
          <thead><tr><th>輪</th><th>時間</th><th>階段</th><th>任務</th><th>訊號</th><th>驗證</th><th>flag</th><th>done</th><th>事件</th></tr></thead>
          <tbody>
            {rows.map((row, index) => (
              <tr key={`${row.round}-${row.time}-${index}`}>
                <td>{row.round}</td>
                <td className="muted">{row.time}</td>
                <td>{row.phase}</td>
                <td>{row.task}</td>
                <td>{row.signal}</td>
                <td>{row.validate === "PASS" ? "✅" : row.validate === "FAIL" ? "❌" : "—"}</td>
                <td>{row.flag}</td>
                <td>{row.done}</td>
                <td>{`${row.tamper ? "⚠️ 竄改 " : ""}${row.agentOk ? "" : "⚠️ Agent 異常 "}${row.event}`}</td>
              </tr>
            ))}
            {!rows.length && !loading && <tr><td colSpan={9} className="table-empty">尚無輪次紀錄</td></tr>}
          </tbody>
        </table>
      </div>
    </Modal>
  );
}
