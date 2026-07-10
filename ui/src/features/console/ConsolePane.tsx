import { useEffect, useRef, useState } from "react";

export default function ConsolePane({
  text,
  round,
  running,
  hasWorkspace
}: {
  text: string;
  round: number;
  running: boolean;
  hasWorkspace: boolean;
}) {
  const consoleRef = useRef<HTMLPreElement>(null);
  const [follow, setFollow] = useState(true);

  useEffect(() => {
    if (follow && consoleRef.current) consoleRef.current.scrollTop = consoleRef.current.scrollHeight;
  }, [text, follow]);

  const onScroll = () => {
    const element = consoleRef.current;
    if (!element) return;
    setFollow(element.scrollTop + element.clientHeight >= element.scrollHeight - 60);
  };

  return (
    <section className="console-pane" aria-label="完整執行紀錄">
      <header className="pane-header console-header">
        <div>
          <strong>完整執行紀錄</strong>
          <span>{hasWorkspace ? `console.log · round ${round}` : "等待 workspace"}</span>
        </div>
        <span className={`live-status ${running ? "running" : "idle"}`}>
          <span aria-hidden="true" />{running ? "live" : "idle"}
        </span>
      </header>
      <pre ref={consoleRef} className="console-output" onScroll={onScroll} tabIndex={0}>
        {text || (hasWorkspace ? "尚無執行紀錄。" : "建立或選擇 workspace 後，完整流程紀錄會顯示在這裡。")}
      </pre>
      {!follow && (
        <button type="button" className="floating-button" onClick={() => setFollow(true)}>
          ⤓ 跟到最新
        </button>
      )}
    </section>
  );
}
