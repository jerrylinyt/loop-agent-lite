import { useEffect, useState } from "react";
import { getJson } from "../../shared/api/client";
import Modal from "../../shared/components/Modal";

interface GoalResponse {
  content?: string;
  path?: string;
  goal_changed?: boolean;
  error?: string;
}

export default function GoalModal({ workspace, onClose }: { workspace: string; onClose: () => void }) {
  const [goal, setGoal] = useState<GoalResponse | null>(null);

  useEffect(() => {
    void (async () => {
      const response = await getJson<GoalResponse>(`/api/goal?ws=${encodeURIComponent(workspace)}`);
      setGoal(response ?? { error: "讀取 goal 失敗" });
    })();
  }, [workspace]);

  return (
    <Modal title="Goal" description={goal?.path ?? "loop 正在為這個 goal 工作（人類真相，唯讀）"} onClose={onClose} wide>
      {!goal ? <div className="loading-state">載入 goal…</div>
        : goal.error ? <div className="loading-state error">{goal.error}</div>
        : <>
            {goal.goal_changed && <div className="goal-warning">⚠ goal 已變更，現有計畫是舊 goal 收斂的，建議回規劃期重新收斂</div>}
            <pre className="report-content">{goal.content}</pre>
          </>}
    </Modal>
  );
}
