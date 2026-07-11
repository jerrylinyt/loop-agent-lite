/** 完成報告檢視器：只讀取 REPORT.md 投影，缺檔與不安全檔案由後端回傳明確錯誤。 */
import { useEffect, useState } from "react";
import { getJson } from "../../shared/api/client";
import Modal from "../../shared/components/Modal";

interface ReportResponse {
  content?: string;
  error?: string;
}

export default function ReportModal({ workspace, onClose }: { workspace: string; onClose: () => void }) {
  const [content, setContent] = useState("");
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    void (async () => {
      const response = await getJson<ReportResponse>(`/api/report?ws=${encodeURIComponent(workspace)}`);
      setLoading(false);
      if (!response || response.error) setError(response?.error ?? "讀取 REPORT.md 失敗");
      else setContent(response.content ?? "");
    })();
  }, [workspace]);

  return (
    <Modal title="完成報告" description="REPORT.md——全部任務收斂後由 loop 產生（唯讀）" onClose={onClose} wide>
      {loading ? <div className="loading-state">載入報告…</div>
        : error ? <div className="loading-state error">{error}</div>
        : <pre className="report-content">{content}</pre>}
    </Modal>
  );
}
