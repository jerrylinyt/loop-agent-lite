/** 垂直分隔線：調整任務與 Agent console 寬度，並提供鍵盤等價操作。 */
import usePointerDrag from "./usePointerDrag";

export default function Splitter({ onResize }: { onResize: (pixels: number) => void }) {
  const resize = (clientX: number) => {
    const minimum = 400;
    const maximum = Math.max(minimum, window.innerWidth - 440);
    onResize(Math.min(maximum, Math.max(minimum, clientX)));
  };
  const dragHandlers = usePointerDrag("resizing", (event) => resize(event.clientX));
  return (
    <div
      className="splitter"
      role="separator"
      aria-label="調整任務與 console 欄寬"
      aria-orientation="vertical"
      tabIndex={0}
      {...dragHandlers}
      onKeyDown={(event) => {
        if (event.key !== "ArrowLeft" && event.key !== "ArrowRight") return;
        event.preventDefault();
        const current = +(localStorage.getItem("left-pane-width") || Math.round(window.innerWidth * 0.44));
        resize(current + (event.key === "ArrowLeft" ? -24 : 24));
      }}
    ><span /></div>
  );
}
