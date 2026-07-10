import { useRef } from "react";

export default function HorizontalSplitter({ onResize }: { onResize: (pixels: number) => void }) {
  const dragging = useRef(false);
  const resize = (clientY: number) => {
    const minimum = 120;
    const maximum = Math.max(minimum, window.innerHeight - 280);
    onResize(Math.min(maximum, Math.max(minimum, window.innerHeight - clientY)));
  };

  return (
    <div
      className="horizontal-splitter"
      role="separator"
      aria-label="調整任務與狀態紀錄高度"
      aria-orientation="horizontal"
      tabIndex={0}
      onPointerDown={(event) => {
        dragging.current = true;
        event.currentTarget.setPointerCapture(event.pointerId);
        document.body.classList.add("resizing-row");
      }}
      onPointerMove={(event) => { if (dragging.current) resize(event.clientY); }}
      onPointerUp={() => { dragging.current = false; document.body.classList.remove("resizing-row"); }}
      onKeyDown={(event) => {
        if (event.key !== "ArrowUp" && event.key !== "ArrowDown") return;
        event.preventDefault();
        const current = +(localStorage.getItem("status-console-height") || 220);
        resize(window.innerHeight - current + (event.key === "ArrowUp" ? -24 : 24));
      }}
    ><span /></div>
  );
}
