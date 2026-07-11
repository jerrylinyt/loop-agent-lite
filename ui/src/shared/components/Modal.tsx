/** 可堆疊 Modal 基礎元件：管理 inert、焦點圈、Esc、Tab trap 與關閉後焦點回復。 */
import { useEffect, useId, useRef, type MouseEvent, type ReactNode } from "react";
import { createPortal } from "react-dom";

export interface ModalProps {
  title: string;
  description?: string;
  onClose: () => void;
  children: ReactNode;
  footer?: ReactNode;
  wide?: boolean;
  extraWide?: boolean;
  compact?: boolean;
  fullScreen?: boolean;
}

const FOCUSABLE = [
  "button:not([disabled])",
  "input:not([disabled]):not([type=hidden])",
  "select:not([disabled])",
  "textarea:not([disabled])",
  "[href]",
  "[tabindex]:not([tabindex='-1'])"
].join(",");

const modalStack: symbol[] = [];

export default function Modal({ title, description, onClose, children, footer, wide, extraWide, compact, fullScreen }: ModalProps) {
  const titleId = useId();
  const panelRef = useRef<HTMLDivElement>(null);
  const previousFocus = useRef<HTMLElement | null>(null);
  const onCloseRef = useRef(onClose);
  const modalId = useRef(Symbol("modal"));

  useEffect(() => { onCloseRef.current = onClose; }, [onClose]);

  useEffect(() => {
    previousFocus.current = document.activeElement as HTMLElement | null;
    const shell = document.getElementById("app-shell");
    const id = modalId.current;
    // stack 只讓最上層 Modal 處理 Esc/Tab；巢狀確認視窗關閉時不會誤關底層編輯器。
    modalStack.push(id);
    shell?.setAttribute("inert", "");
    const panel = panelRef.current;
    const initial = panel?.querySelector<HTMLElement>("[data-autofocus]")
      ?? panel?.querySelector<HTMLElement>(FOCUSABLE);
    initial?.focus();

    const onKeyDown = (event: KeyboardEvent) => {
      if (modalStack[modalStack.length - 1] !== id) return;
      if (event.key === "Escape") {
        event.preventDefault();
        onCloseRef.current();
        return;
      }
      if (event.key !== "Tab" || !panel) return;
      // offsetParent 過濾 display:none/隱藏控制，避免焦點被困在不可見元素。
      const controls = [...panel.querySelectorAll<HTMLElement>(FOCUSABLE)]
        .filter((element) => element.offsetParent !== null);
      if (!controls.length) return;
      const first = controls[0];
      const last = controls[controls.length - 1];
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    };
    document.addEventListener("keydown", onKeyDown);
    return () => {
      document.removeEventListener("keydown", onKeyDown);
      const index = modalStack.lastIndexOf(id);
      if (index >= 0) modalStack.splice(index, 1);
      if (modalStack.length === 0) shell?.removeAttribute("inert");
      previousFocus.current?.focus();
    };
  }, []);

  const stop = (event: MouseEvent) => event.stopPropagation();
  return createPortal(
    <div className="modal-backdrop" onMouseDown={onClose}>
      <div
        ref={panelRef}
        className={`modal${wide ? " modal-wide" : ""}${extraWide ? " modal-extra-wide" : ""}${compact ? " modal-compact" : ""}${fullScreen ? " modal-full-screen" : ""}`}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        aria-describedby={description ? `${titleId}-description` : undefined}
        onMouseDown={stop}
      >
        <header className="modal-header">
          <div>
            <h2 id={titleId}>{title}</h2>
            {description && <p id={`${titleId}-description`}>{description}</p>}
          </div>
          <button type="button" className="icon-button" onClick={onClose} aria-label="關閉對話框">✕</button>
        </header>
        <div className="modal-body">{children}</div>
        {footer && <footer className="modal-footer">{footer}</footer>}
      </div>
    </div>,
    document.body
  );
}
