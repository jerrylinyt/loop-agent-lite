/** 最近一輪 prompt 的唯讀檢視器；內容來自 workspace 安全投影，不允許前端修改。 */
import { useEffect, useState } from "react";
import { getJson } from "../../shared/api/client";
import Modal from "../../shared/components/Modal";

interface PromptResponse {
  content?: string;
  round?: number;
  file?: string;
  error?: string;
}

export default function PromptModal({ workspace, onClose }: { workspace: string; onClose: () => void }) {
  const [prompt, setPrompt] = useState<PromptResponse | null>(null);

  useEffect(() => {
    void (async () => {
      const response = await getJson<PromptResponse>(`/api/prompt?ws=${encodeURIComponent(workspace)}`);
      setPrompt(response ?? { error: "讀取 prompt 失敗" });
    })();
  }, [workspace]);

  return (
    <Modal
      title="最近一輪 Prompt"
      description={prompt?.file ? `第 ${prompt.round} 輪送給 Agent 的完整輸入（${prompt.file}，唯讀）` : "loop 送給 Agent 的完整輸入（唯讀）"}
      onClose={onClose}
      wide
    >
      {!prompt ? <div className="loading-state">載入 prompt…</div>
        : prompt.error ? <div className="loading-state error">{prompt.error}</div>
        : <pre className="report-content">{prompt.content}</pre>}
    </Modal>
  );
}
