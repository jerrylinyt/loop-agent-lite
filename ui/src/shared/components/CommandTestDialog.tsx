import Modal from "./Modal";

export interface CommandTestState {
  loading: boolean;
  ok?: boolean;
  text?: string;
  output?: string;
}

export default function CommandTestDialog({ state, onClose }: { state: CommandTestState; onClose: () => void }) {
  return (
    <Modal
      title="Agent CLI 執行確認"
      description="固定測試 prompt：test"
      compact
      onClose={onClose}
      footer={!state.loading && <button type="button" className="primary-button" onClick={onClose} data-autofocus>關閉</button>}
    >
      <div className="agent-test-result" role="status" aria-live="polite">
        {state.loading ? (
          <div className="command-loading"><span className="spinner" aria-hidden="true" /><span>Agent CLI 執行中，請稍候…</span></div>
        ) : (
          <><strong className={state.ok ? "result-success" : "result-error"}>{state.text}</strong><pre>{`result\n${state.output || "（無輸出）"}`}</pre></>
        )}
      </div>
    </Modal>
  );
}
