/** 水平分隔線：支援滑鼠拖曳與方向鍵微調，尺寸範圍由元件內限制避免面板消失。 */
import usePointerDrag from "./usePointerDrag";

export default function HorizontalSplitter({ onResize }: { onResize: (pixels: number) => void }) {
  const resize = (clientY: number) => {
    const minimum = 120;
    const maximum = Math.max(minimum, window.innerHeight - 280);
    onResize(Math.min(maximum, Math.max(minimum, window.innerHeight - clientY)));
  };
  const dragHandlers = usePointerDrag("resizing-row", (event) => resize(event.clientY));

  return (
    <div
      className="horizontal-splitter"
      role="separator"
      aria-label="調整任務與狀態紀錄高度"
      aria-orientation="horizontal"
      tabIndex={0}
      {...dragHandlers}
      onKeyDown={(event) => {
        if (event.key !== "ArrowUp" && event.key !== "ArrowDown") return;
        event.preventDefault();
        const current = +(localStorage.getItem("status-console-height") || 220);
        resize(window.innerHeight - current + (event.key === "ArrowUp" ? -24 : 24));
      }}
    ><span /></div>
  );
}
