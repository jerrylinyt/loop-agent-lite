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
    <section className="console-pane" aria-label="Agent console">
      <header className="pane-header console-header">
        <div>
          <strong>Agent console</strong>
          <span>{hasWorkspace ? `round ${round}` : "等待 workspace"}</span>
        </div>
        <span className={`live-status ${running ? "running" : "idle"}`}>
          <span aria-hidden="true" />{running ? "live" : "idle"}
        </span>
      </header>
      <pre ref={consoleRef} className="console-output" onScroll={onScroll} tabIndex={0}>
        {text || (hasWorkspace ? "尚無 console 輸出。" : "建立或選擇 workspace 後，agent 輸出會顯示在這裡。")}
      </pre>
      {!follow && (
        <button type="button" className="floating-button" onClick={() => setFollow(true)}>
          ⤓ 跟到最新
        </button>
      )}
    </section>
  );
}
