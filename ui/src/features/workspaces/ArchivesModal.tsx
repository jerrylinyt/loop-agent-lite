import { useCallback, useEffect, useState } from "react";
import { getJson, postJson } from "../../shared/api/client";
import type { ArchiveSummary, ArchivesResponse, DeleteArchiveResponse, RestoreArchiveResponse } from "../../shared/api/types";
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
  const [pendingDelete, setPendingDelete] = useState<ArchiveSummary | null>(null);
  const [restoring, setRestoring] = useState("");
  const [deleting, setDeleting] = useState("");

  const load = useCallback(async () => {
    setLoading(true);
    const response = await getJson<ArchivesResponse>("/api/archives");
    setArchives(response?.archives ?? []);
    setError(response?.error ?? (response ? "" : "無法讀取封存清單"));
    setLoading(false);
  }, []);

  useEffect(() => { void load(); }, [load]);

  const restore = async (archive: ArchiveSummary) => {
    if (restoring || deleting) return;
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

  const deleteArchive = async (archive: ArchiveSummary) => {
    if (restoring || deleting) return;
    setDeleting(archive.id);
    try {
      const response = await postJson<DeleteArchiveResponse>("/api/delete-archive", { archive_id: archive.id });
      if (response.error) {
        setError(response.error);
        return;
      }
      await load();
    } finally {
      setPendingDelete(null);
      setDeleting("");
    }
  };

  return (
    <>
      <Modal
        title="已封存 workspace"
        description={readonly ? "可查看封存內容；唯讀模式不可還原或刪除。" : "還原只搬回 coordinator state；永久刪除不可復原。"}
        onClose={onClose}
        wide
        footer={<><button type="button" className="secondary-button" onClick={() => void load()} disabled={loading || !!restoring || !!deleting}>{loading ? "載入中…" : "重新整理"}</button><span role="status" className="muted">{error}</span></>}
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
                      {!readonly && <td className="archive-actions"><button type="button" className="secondary-button" disabled={!!restoring || !!deleting} onClick={() => setPending(archive)}>{restoring === archive.id ? "還原中…" : `還原 ${archive.name}`}</button><button type="button" className="danger-button" disabled={!!restoring || !!deleting} onClick={() => setPendingDelete(archive)}>{deleting === archive.id ? "刪除中…" : "永久刪除"}</button></td>}
                    </tr>)}
                  </tbody>
                </table>
              </div>}
      </Modal>
      {pending && <ActionDialog
        title="確認還原"
        message={`還原 ${pending.name}？這會把完整 workspace 搬回工作區，但不會自動啟動 loop。`}
        confirmLabel="還原"
        preview={[
          { label: "來源", value: pending.id },
          { label: "還原位置", value: `workspace/${pending.name}` },
          { label: "啟動行為", value: "只還原資料，不會自動啟動 loop", tone: "safe" },
        ]}
        onClose={() => !restoring && setPending(null)}
        onConfirm={() => void restore(pending)}
      />}
      {pendingDelete && <ActionDialog
        title="確認永久刪除"
        message={`永久刪除 ${pendingDelete.name} 的封存？完整 workspace 會被移除，無法還原；target repo 與程式碼不受影響。`}
        confirmLabel="永久刪除"
        danger
        preview={[
          { label: "永久刪除", value: pendingDelete.id, tone: "warning" },
          { label: "包含", value: "state、history、console、prompt、異常 log 與完成報告" },
          { label: "不受影響", value: "target repo 與程式碼", tone: "safe" },
        ]}
        onClose={() => !deleting && setPendingDelete(null)}
        onConfirm={() => void deleteArchive(pendingDelete)}
      />}
    </>
  );
}
