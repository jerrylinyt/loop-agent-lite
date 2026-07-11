/** 共用 pointer 拖曳生命週期；尺寸計算留給各 splitter，這裡只管理 capture 與清理。 */
import { useEffect, useRef } from "react";
import type { PointerEvent as ReactPointerEvent, PointerEventHandler } from "react";

export default function usePointerDrag(bodyClass: string, onMove: (event: ReactPointerEvent) => void): {
  onPointerDown: PointerEventHandler<HTMLDivElement>;
  onPointerMove: PointerEventHandler<HTMLDivElement>;
  onPointerUp: PointerEventHandler<HTMLDivElement>;
  onPointerCancel: PointerEventHandler<HTMLDivElement>;
} {
  const dragging = useRef(false);
  const moveRef = useRef(onMove);
  moveRef.current = onMove;

  const finish: PointerEventHandler<HTMLDivElement> = () => {
    dragging.current = false;
    document.body.classList.remove(bodyClass);
  };

  useEffect(() => () => {
    // Modal/版面切換若發生在拖曳中，不能把全頁 cursor/user-select 狀態留在 body。
    document.body.classList.remove(bodyClass);
  }, [bodyClass]);

  return {
    onPointerDown: (event) => {
      dragging.current = true;
      event.currentTarget.setPointerCapture(event.pointerId);
      document.body.classList.add(bodyClass);
    },
    onPointerMove: (event) => {
      if (dragging.current) moveRef.current(event);
    },
    onPointerUp: finish,
    onPointerCancel: finish,
  };
}
