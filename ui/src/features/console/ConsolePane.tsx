/** Console 顯示器：依來源與文字過濾長 log、保留捲動位置，並交給 ANSI parser 做受限上色。 */
import { useEffect, useMemo, useRef, useState } from "react";
import { hasAnsi, renderAnsi } from "./ansi";
import { filterConsoleText, searchConsoleText, withoutEmojiIcons, type ConsoleFilter } from "./consoleText";

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
  const [search, setSearch] = useState("");
  const visibleText = useMemo(
    () => searchConsoleText(filterConsoleText(text, showFilters ? filter : defaultFilter), search),
    [text, filter, showFilters, defaultFilter, search]
  );
  const renderedText = useMemo(
    () => {
      const displayText = withoutEmojiIcons(visibleText);
      return hasAnsi(displayText) ? renderAnsi(displayText) : displayText;
    },
    [visibleText]
  );

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
          <strong>{title}</strong>
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
          <input
            type="search"
            className="console-search"
            aria-label={`過濾${title}`}
            placeholder="過濾…"
            value={search}
            onChange={(event) => setSearch(event.target.value)}
          />
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
          {onToggleCollapse && <button type="button" className="text-button console-collapse-button" onClick={onToggleCollapse} aria-label={`收合${title}`} title={`收合${title}`}>收合</button>}
        </div>
      </header>
      <pre ref={consoleRef} className="console-output" onScroll={onScroll} tabIndex={0}>
        {visibleText ? renderedText : (search.trim() ? "沒有符合過濾條件的行。"
          : hasWorkspace ? "此分類尚無執行紀錄。" : "建立或選擇 workspace 後，執行紀錄會顯示在這裡。")}
      </pre>
      {!follow && (
        <button type="button" className="floating-button" onClick={() => setFollow(true)}>
          跟到最新
        </button>
      )}
    </section>
  );
}
