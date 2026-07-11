import { useRef, useState } from "react";
import { getJson } from "../../shared/api/client";
import Modal from "../../shared/components/Modal";
import type { GlobalSearchResponse } from "../../shared/api/types";

const KIND_NAMES: Record<string, string> = {
  state: "State", task: "任務", issue: "Issue", commit: "Commit",
  history: "輪次", console: "Console", anomaly: "異常 Log"
};

export default function GlobalSearchModal({ onClose, onSelect }: {
  onClose: () => void;
  onSelect: (workspace: string) => void;
}) {
  const [query, setQuery] = useState("");
  const [projection, setProjection] = useState<GlobalSearchResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const requestSeq = useRef(0);

  const search = async () => {
    const value = query.trim();
    if (value.length < 2) {
      setProjection({ error: "請輸入至少 2 個字", query: value, limit: 100, count: 0, truncated: false, results: [] });
      return;
    }
    const seq = requestSeq.current + 1;
    requestSeq.current = seq;
    setLoading(true);
    const response = await getJson<GlobalSearchResponse>(`/api/search?q=${encodeURIComponent(value)}&limit=100`);
    if (seq !== requestSeq.current) return;
    setLoading(false);
    setProjection(response ?? { error: "搜尋連線失敗", query: value, limit: 100, count: 0, truncated: false, results: [] });
  };

  return <Modal title="全域搜尋" description="跨 workspace 搜尋 task、issue、commit、history、console 與近期 anomaly log" onClose={onClose} extraWide>
    <form className="global-search-form" onSubmit={(event) => { event.preventDefault(); void search(); }}>
      <input type="search" aria-label="全域搜尋文字" placeholder="輸入至少 2 個字、task 名稱或 commit SHA…" value={query} onChange={(event) => { requestSeq.current += 1; setQuery(event.target.value); setProjection(null); setLoading(false); }} data-autofocus />
      <button type="submit" className="primary-button" disabled={loading || query.trim().length < 2}>{loading ? "搜尋中…" : "搜尋"}</button>
    </form>
    {projection?.error && <p className="field-error" role="alert">{projection.error}</p>}
    {projection && !projection.error && <div className="global-search-summary">
      <span>找到 {projection.count} 筆</span>{projection.truncated && <span className="warning">已達掃描或結果上限，請縮小關鍵字</span>}
    </div>}
    <div className="global-search-results">
      {projection?.results.map((result) => <button type="button" className="global-search-result" key={result.id} onClick={() => onSelect(result.workspace)}>
        <span className={`search-kind kind-${result.kind}`}>{KIND_NAMES[result.kind] ?? result.kind}</span>
        <div><header><strong>{result.workspace}</strong><b>{result.title}</b>{result.round !== undefined && <small>round {result.round}</small>}</header><p>{result.snippet}</p></div>
        <i>開啟 workspace →</i>
      </button>)}
      {projection && !projection.error && !projection.results.length && <div className="loading-state">沒有符合的結果</div>}
      {!projection && !loading && <div className="loading-state">搜尋結果會顯示來源與上下文；點擊可直接前往 workspace。</div>}
    </div>
  </Modal>;
}
