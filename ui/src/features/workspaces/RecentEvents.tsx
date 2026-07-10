import { useState } from "react";

export default function RecentEvents({ lines }: { lines: string[] }) {
  const [expanded, setExpanded] = useState(() => localStorage.getItem("events-expanded") !== "0");
  const toggle = () => {
    const next = !expanded;
    setExpanded(next);
    localStorage.setItem("events-expanded", next ? "1" : "0");
  };
  return (
    <section className={`recent-events${expanded ? "" : " collapsed"}`}>
      <button type="button" className="event-toggle" onClick={toggle} aria-expanded={expanded}>
        <span>{expanded ? "▾" : "▸"} 最近事件</span>
        <span className="event-count">{lines.length}</span>
      </button>
      {expanded && (
        <pre className="event-output" tabIndex={0}>
          {lines.length ? lines.join("\n") : "尚無事件"}
        </pre>
      )}
    </section>
  );
}
