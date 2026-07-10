import Modal from "./Modal";

export default function ActionDialog({
  title,
  message,
  confirmLabel = "確定",
  cancelLabel = "取消",
  danger = false,
  onConfirm,
  onClose
}: {
  title: string;
  message: string;
  confirmLabel?: string;
  cancelLabel?: string;
  danger?: boolean;
  onConfirm?: () => void;
  onClose: () => void;
}) {
  return (
    <Modal
      title={title}
      onClose={onClose}
      footer={onConfirm ? (
        <>
          <button type="button" className="secondary-button" onClick={onClose}>{cancelLabel}</button>
          <button type="button" className={danger ? "danger-button" : "primary-button"} onClick={onConfirm} data-autofocus>{confirmLabel}</button>
        </>
      ) : (
        <button type="button" className="primary-button" onClick={onClose} data-autofocus>{confirmLabel}</button>
      )}
    >
      <p className="dialog-message">{message}</p>
    </Modal>
  );
}
