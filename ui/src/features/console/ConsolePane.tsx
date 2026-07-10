import { useEffect, useMemo, useRef, useState } from "react";

export type ConsoleFilter = "agent" | "other" | "all";

export function filterConsoleText(text: string, filter: ConsoleFilter) {
  if (filter === "all") return text;
  const wantAgent = filter === "agent";
  return text.split("\n").filter((line) => line.includes("🤖 Agent｜") === wantAgent).join("\n");
}

export default function ConsolePane({
  text,
  round,
  running,
  hasWorkspace,
  title = "Agent 執行輸出",
  ariaLabel = "Agent 執行輸出",
  defaultFilter = "agent",
  showFilters = true,
  collapsed = false,
  onToggleCollapse
}: {
  text: string;
  round: number;
  running: boolean;
  hasWorkspace: boolean;
  title?: string;
  ariaLabel?: string;
  defaultFilter?: ConsoleFilter;
  showFilters?: boolean;
  collapsed?: boolean;
  onToggleCollapse?: () => void;
}) {
  const consoleRef = useRef<HTMLPreElement>(null);
  const [follow, setFollow] = useState(true);
  const [filter, setFilter] = useState<ConsoleFilter>(defaultFilter);
  const visibleText = useMemo(() => filterConsoleText(text, showFilters ? filter : defaultFilter), [text, filter, showFilters, defaultFilter]);

  useEffect(() => {
    if (follow && consoleRef.current) consoleRef.current.scrollTop = consoleRef.current.scrollHeight;
  }, [visibleText, follow]);

  const onScroll = () => {
    const element = consoleRef.current;
    if (!element) return;
    setFollow(element.scrollTop + element.clientHeight >= element.scrollHeight - 60);
  };

  if (collapsed) {
    return (
      <section className="console-pane console-collapsed" aria-label={`${ariaLabel}（已收合）`}>
        <button type="button" className="console-expand-button" onClick={onToggleCollapse} aria-label={`展開${title}`} title={`展開${title}`}>
          <span aria-hidden="true">‹</span><strong>{title}</strong>
        </button>
      </section>
    );
  }

  return (
    <section className="console-pane" aria-label={ariaLabel}>
      <header className="pane-header console-header">
        <div className="console-heading">
          <strong>{title}</strong>
          <span>{hasWorkspace ? `console.log · round ${round}` : "等待 workspace"}</span>
        </div>
        <div className="console-tools">
          {showFilters && (
            <div className="console-filters" role="group" aria-label="紀錄篩選">
              {(["agent", "other", "all"] as const).map((value) => (
                <button key={value} type="button" className={filter === value ? "active" : ""} aria-pressed={filter === value} onClick={() => setFilter(value)}>
                  {{ agent: "Agent", other: "其他", all: "全部" }[value]}
                </button>
              ))}
            </div>
          )}
          <span className={`live-status ${running ? "running" : "idle"}`}>
            <span aria-hidden="true" />{running ? "live" : "idle"}
          </span>
          {onToggleCollapse && <button type="button" className="icon-button console-collapse-button" onClick={onToggleCollapse} aria-label={`收合${title}`} title={`收合${title}`}>›</button>}
        </div>
      </header>
      <pre ref={consoleRef} className="console-output" onScroll={onScroll} tabIndex={0}>
        {visibleText || (hasWorkspace ? "此分類尚無執行紀錄。" : "建立或選擇 workspace 後，執行紀錄會顯示在這裡。")}
      </pre>
      {!follow && (
        <button type="button" className="floating-button" onClick={() => setFollow(true)}>
          ⤓ 跟到最新
        </button>
      )}
    </section>
  );
}
