import { useCallback, useEffect, useState } from "react";
import { getJson, postJson } from "../../shared/api/client";
import type { ArchiveSummary, ArchivesResponse, RestoreArchiveResponse } from "../../shared/api/types";
import ActionDialog from "../../shared/components/ActionDialog";
import Modal from "../../shared/components/Modal";

export default function ArchivesModal({
  readonly,
  onClose,
  onRestored
}: {
  readonly: boolean;
  onClose: () => void;
  onRestored: (name: string) => void | Promise<void>;
}) {
  const [archives, setArchives] = useState<ArchiveSummary[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [pending, setPending] = useState<ArchiveSummary | null>(null);
  const [restoring, setRestoring] = useState("");

  const load = useCallback(async () => {
    setLoading(true);
    const response = await getJson<ArchivesResponse>("/api/archives");
    setArchives(response?.archives ?? []);
    setError(response?.error ?? (response ? "" : "無法讀取封存清單"));
    setLoading(false);
  }, []);

  useEffect(() => { void load(); }, [load]);

  const restore = async (archive: ArchiveSummary) => {
    if (restoring) return;
    setRestoring(archive.id);
    try {
      const response = await postJson<RestoreArchiveResponse>("/api/restore-workspace", { archive_id: archive.id });
      if (response.error) {
        setError(response.error);
        return;
      }
      await Promise.resolve(onRestored(response.name ?? archive.name));
    } finally {
      setPending(null);
      setRestoring("");
    }
  };

  return (
    <>
      <Modal
        title="已封存 workspace"
        description={readonly ? "可查看封存內容；唯讀模式不可還原。" : "還原只搬回 coordinator state，不會自動啟動 loop。"}
        onClose={onClose}
        wide
        footer={<><button type="button" className="secondary-button" onClick={() => void load()} disabled={loading || !!restoring}>{loading ? "載入中…" : "重新整理"}</button><span role="status" className="muted">{error}</span></>}
      >
        {loading ? <div className="loading-state">讀取封存清單…</div>
          : !archives.length ? <div className="loading-state">目前沒有已封存的 workspace</div>
            : <div className="modal-table-scroll">
                <table>
                  <thead><tr><th>workspace</th><th>封存時間</th><th>狀態</th>{!readonly && <th>操作</th>}</tr></thead>
                  <tbody>
                    {archives.map((archive) => <tr key={archive.id}>
                      <td><strong>{archive.name}</strong>{archive.legacy && <div className="muted">舊版封存格式</div>}</td>
                      <td>{archive.archived_at}</td>
                      <td>{archive.phase ?? "—"}{typeof archive.round === "number" ? ` · round ${archive.round}` : ""}</td>
                      {!readonly && <td><button type="button" className="secondary-button" disabled={!!restoring} onClick={() => setPending(archive)}>{restoring === archive.id ? "還原中…" : `還原 ${archive.name}`}</button></td>}
                    </tr>)}
                  </tbody>
                </table>
              </div>}
      </Modal>
      {pending && <ActionDialog
        title="確認還原"
        message={`還原 ${pending.name}？這會把完整 workspace 搬回工作區，但不會自動啟動 loop。`}
        confirmLabel="還原"
        onClose={() => !restoring && setPending(null)}
        onConfirm={() => void restore(pending)}
      />}
    </>
  );
}
