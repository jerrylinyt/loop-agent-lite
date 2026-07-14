/** Completed task 的 Git 變更瀏覽器：左側選檔，右側以完整 task commit 範圍呈現 patch。 */
import { useEffect, useMemo, useState } from "react";
import { DiffModeEnum, DiffView } from "@git-diff-view/react";
import "@git-diff-view/react/styles/diff-view-pure.css";
import { getJson } from "../../shared/api/client";
import type { TaskDiffFile, TaskDiffResponse } from "../../shared/api/types";
import Modal from "../../shared/components/Modal";
import useStaleGuard from "../../shared/hooks/useStaleGuard";

const STATUS_LABELS: Record<TaskDiffFile["status"], string> = {
  added: "新增", deleted: "刪除", modified: "修改", renamed: "改名",
  copied: "複製", type_changed: "類型", unmerged: "衝突", unknown: "變更",
};

const MODE_LABELS = {
  task_range: "已記錄 task 起點",
  previous_task: "前一 task SHA 推導",
  single_commit: "單一完成 commit",
};

function shortSha(value?: string | null): string {
  return value?.slice(0, 8) ?? "—";
}

function displayDate(value: string): string {
  const parsed = new Date(value);
  return Number.isNaN(parsed.valueOf()) ? value : parsed.toLocaleString("zh-TW", { hour12: false });
}

function pathParts(path: string): { directory: string; name: string } {
  const split = path.lastIndexOf("/");
  return split < 0 ? { directory: "", name: path }
    : { directory: path.slice(0, split + 1), name: path.slice(split + 1) };
}

export default function TaskDiffModal({ workspace, order, fallbackTitle, fallbackSha, onClose }: {
  workspace: string;
  order: number;
  fallbackTitle: string;
  fallbackSha: string;
  onClose: () => void;
}) {
  const [summary, setSummary] = useState<TaskDiffResponse | null>(null);
  const [summaryLoading, setSummaryLoading] = useState(true);
  const [selectedPath, setSelectedPath] = useState("");
  const [fileDiff, setFileDiff] = useState<TaskDiffResponse | null>(null);
  const [fileLoading, setFileLoading] = useState(false);
  const [filter, setFilter] = useState("");
  const [layout, setLayout] = useState<"split" | "unified">(() =>
    localStorage.getItem("task-diff-layout") === "unified" ? "unified" : "split"
  );
  const [wrap, setWrap] = useState(() => localStorage.getItem("task-diff-wrap") === "1");
  const fileGuard = useStaleGuard();

  useEffect(() => {
    let active = true;
    setSummaryLoading(true);
    void getJson<TaskDiffResponse>(
      `/api/task-diff?ws=${encodeURIComponent(workspace)}&order=${order}`
    ).then((response) => {
      if (!active) return;
      const next = response ?? { error: "Git diff 讀取失敗" };
      setSummary(next);
      setSelectedPath(next.files?.[0]?.path ?? "");
      setSummaryLoading(false);
    });
    return () => { active = false; };
  }, [order, workspace]);

  const files = useMemo(() => summary?.files ?? [], [summary?.files]);
  const selectedFile = useMemo(
    () => files.find((file) => file.path === selectedPath) ?? null,
    [files, selectedPath]
  );
  useEffect(() => {
    fileGuard.cancelPending();
    if (!selectedFile) {
      setFileDiff(null);
      setFileLoading(false);
      return;
    }
    if (selectedFile.binary) {
      setFileDiff({ selected_file: selectedFile, patch: "" });
      setFileLoading(false);
      return;
    }
    const isCurrent = fileGuard.begin();
    setFileDiff(null);
    setFileLoading(true);
    void getJson<TaskDiffResponse>(
      `/api/task-diff?ws=${encodeURIComponent(workspace)}&order=${order}&file=${encodeURIComponent(selectedFile.path)}`
    ).then((response) => {
      if (!isCurrent()) return;
      setFileDiff(response ?? { error: "檔案 diff 讀取失敗" });
      setFileLoading(false);
    });
  }, [fileGuard, order, selectedFile, workspace]);

  const visibleFiles = useMemo(() => {
    const needle = filter.trim().toLowerCase();
    return needle ? files.filter((file) => `${file.old_path ?? ""} ${file.path}`.toLowerCase().includes(needle)) : files;
  }, [files, filter]);

  const changeLayout = (next: "split" | "unified") => {
    setLayout(next);
    localStorage.setItem("task-diff-layout", next);
  };
  const changeWrap = (next: boolean) => {
    setWrap(next);
    localStorage.setItem("task-diff-wrap", next ? "1" : "0");
  };
  const comparison = summary?.comparison;
  const stats = summary?.stats;
  const commits = summary?.commits ?? [];
  const title = summary?.task?.title ?? fallbackTitle;
  const themeType = document.documentElement.dataset.theme === "light" ? "light" : "dark";
  const patchData = useMemo(() => fileDiff?.patch && selectedFile ? {
    oldFile: { fileName: selectedFile.old_path ?? selectedFile.path },
    newFile: { fileName: selectedFile.path },
    hunks: [fileDiff.patch],
  } : null, [fileDiff?.patch, selectedFile]);

  return (
    <Modal
      title={`task-${order}｜Git 變更`}
      description={title}
      onClose={onClose}
      fullScreen
      bodyClassName="task-diff-modal-body"
    >
      {summaryLoading ? <div className="task-diff-loading">正在重建 task commit 範圍…</div>
        : summary?.error ? <div className="task-diff-loading error">{summary.error}</div>
        : <div className="task-diff-browser">
            <header className="task-diff-summary">
              <div className="task-diff-range">
                <span className={`task-diff-mode mode-${comparison?.mode ?? "single_commit"}`}>
                  {comparison ? MODE_LABELS[comparison.mode] : "完成 SHA"}
                </span>
                <code title={comparison?.base_sha ?? undefined}>{shortSha(comparison?.base_sha)}</code>
                <span aria-hidden="true">→</span>
                <code title={comparison?.head_sha ?? fallbackSha}>{shortSha(comparison?.head_sha ?? fallbackSha)}</code>
                <span className="muted">{commits.length} commits</span>
              </div>
              <div className="task-diff-totals" aria-label="Task Git 變更統計">
                <span>{stats?.files ?? 0} files</span>
                <strong className="diff-add">+{stats?.additions ?? 0}</strong>
                <strong className="diff-delete">−{stats?.deletions ?? 0}</strong>
                {!!stats?.binary_files && <span>{stats.binary_files} binary</span>}
              </div>
              <div className="task-diff-controls">
                <div className="segmented-tabs task-diff-tabs" role="tablist" aria-label="Diff 排版">
                  <button type="button" role="tab" aria-selected={layout === "split"} className={layout === "split" ? "active" : ""} onClick={() => changeLayout("split")}>並排</button>
                  <button type="button" role="tab" aria-selected={layout === "unified"} className={layout === "unified" ? "active" : ""} onClick={() => changeLayout("unified")}>單欄</button>
                </div>
                <label><input type="checkbox" checked={wrap} onChange={(event) => changeWrap(event.target.checked)} /> 自動換行</label>
              </div>
            </header>
            {comparison?.warning && <div className="task-diff-warning">{comparison.warning}</div>}
            {comparison?.mode === "single_commit" && <div className="task-diff-warning">此為舊完成紀錄，找不到可靠 task 起點；目前僅顯示完成 SHA 對應的單一 commit。</div>}
            <details className="task-commit-list">
              <summary>這個 task 包含 {commits.length} 個 commit</summary>
              <ol>
                {commits.map((commit) => <li key={commit.sha}>
                  <code title={commit.sha}>{commit.short_sha}</code>
                  <span>{commit.subject}</span>
                  <small>{commit.author} · {displayDate(commit.date)}</small>
                </li>)}
                {!commits.length && <li className="muted">起點與完成 SHA 相同，沒有額外 commit。</li>}
              </ol>
            </details>
            <div className="task-diff-main">
              <aside className="task-diff-files" aria-label="變更檔案">
                <div className="task-diff-file-search">
                  <input type="search" aria-label="搜尋變更檔案" placeholder="搜尋檔案…" value={filter} onChange={(event) => setFilter(event.target.value)} />
                  <span>{visibleFiles.length} / {files.length}</span>
                </div>
                <div className="task-diff-file-list">
                  {visibleFiles.map((file) => {
                    const parts = pathParts(file.path);
                    return <button key={`${file.old_path ?? ""}->${file.path}`} type="button"
                      className={selectedPath === file.path ? "active" : ""}
                      aria-pressed={selectedPath === file.path}
                      aria-label={`${file.path}，${STATUS_LABELS[file.status]}`}
                      onClick={() => setSelectedPath(file.path)}>
                      <span className={`task-file-status status-${file.status}`}>{file.status_code}</span>
                      <span className="task-file-path">
                        <span><small>{parts.directory}</small><strong>{parts.name}</strong></span>
                        {file.old_path && <em title={file.old_path}>原：{file.old_path}</em>}
                      </span>
                      <span className="task-file-stats">
                        {file.binary ? <i>BIN</i> : <><b>+{file.additions ?? 0}</b><em>−{file.deletions ?? 0}</em></>}
                      </span>
                    </button>;
                  })}
                  {!visibleFiles.length && <div className="empty-inline">{files.length ? "找不到符合的檔案" : "此 task 沒有檔案淨變更"}</div>}
                </div>
              </aside>
              <section className="task-diff-viewer" aria-label="檔案 Git diff">
                {!selectedFile && <div className="task-diff-loading">{files.length ? "請從左側選擇檔案" : "起點與完成 SHA 的檔案內容相同。"}</div>}
                {selectedFile && <header>
                  <div><strong>{selectedFile.path}</strong>{selectedFile.old_path && <span>由 {selectedFile.old_path} 改名</span>}</div>
                  <span>{STATUS_LABELS[selectedFile.status]}{selectedFile.similarity !== null && selectedFile.similarity !== undefined ? ` · ${selectedFile.similarity}%` : ""}</span>
                </header>}
                {selectedFile?.binary && <div className="task-diff-loading">Binary 檔案不提供文字 diff。Git 已記錄此檔案發生變更。</div>}
                {selectedFile && !selectedFile.binary && fileLoading && <div className="task-diff-loading">載入 {selectedFile.path}…</div>}
                {fileDiff?.error && <div className="task-diff-loading error">{fileDiff.error}</div>}
                {fileDiff?.patch_too_large && <div className="task-diff-loading error">此單一檔案 patch 超過 {(fileDiff.patch_limit_bytes ?? 0) / 1024 / 1024} MiB 安全上限，未在瀏覽器展開。</div>}
                {selectedFile && !selectedFile.binary && fileDiff?.patch !== undefined && !fileDiff.patch_too_large &&
                  (patchData ? <div className={`task-patch-view${wrap ? " wrap" : ""}`}>
                    <DiffView
                      key={`${selectedFile.path}-${layout}-${wrap}-${themeType}`}
                      data={patchData}
                      diffViewMode={layout === "split" ? DiffModeEnum.Split : DiffModeEnum.Unified}
                      diffViewTheme={themeType}
                      diffViewWrap={wrap}
                      diffViewHighlight
                      diffViewFontSize={12}
                    />
                  </div> : <div className="task-diff-loading">這個檔案在所選範圍內沒有文字 patch。</div>)}
              </section>
            </div>
          </div>}
    </Modal>
  );
}
