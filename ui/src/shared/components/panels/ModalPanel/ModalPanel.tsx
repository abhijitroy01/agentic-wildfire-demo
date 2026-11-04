import { useBodyScrollLock } from "@/shared/hooks/useBodyScrollLock";
import { useFocusTrap } from "@/shared/hooks/useFocusTrap";
import type React from "react";
import { memo,useCallback } from "react";
import { PANEL_Z_INDEX } from "../constants";
import type { ModalPanelProps } from "../types";
import "./ModalPanel.css";

const ModalPanel: React.FC<ModalPanelProps> = memo(
  ({
    id: _id,
    isOpen,
    onClose,
    title,
    icon,
    children,
    className,
    maxWidth = "900px",
    closeOnBackdropClick = true,
    closeOnEscape = true,
    showCloseButton = true,
    footer,
    "aria-label": ariaLabel,
  }) => {
    // Focus trap using existing shared hook
    const containerRef = useFocusTrap<HTMLDivElement>({
      isActive: isOpen,
      returnFocusOnDeactivate: true,
      onEscapeAttempt: closeOnEscape ? onClose : undefined,
    });

    // Body scroll lock
    useBodyScrollLock(isOpen);

    // Backdrop click: safe e.target === e.currentTarget pattern
    const handleBackdropClick = useCallback(
      (e: React.MouseEvent) => {
        if (closeOnBackdropClick && e.target === e.currentTarget) {
          onClose();
        }
      },
      [closeOnBackdropClick, onClose]
    );

    if (!isOpen) return null;

    return (
      <div
        className="modal-panel__backdrop"
        onClick={handleBackdropClick}
        style={{ zIndex: PANEL_Z_INDEX.MODAL_BACKDROP }}
      >
        <div
          ref={containerRef}
          className={`modal-panel ${className || ""}`}
          role="dialog"
          aria-modal="true"
          aria-label={ariaLabel || title}
          style={{
            zIndex: PANEL_Z_INDEX.MODAL_CONTENT,
            maxWidth,
          }}
        >
          <div className="modal-panel__header">
            <div className="modal-panel__title-row">
              {icon && (
                <span className="modal-panel__icon" aria-hidden="true">
                  {icon}
                </span>
              )}
              <h2 className="modal-panel__title">{title}</h2>
            </div>
            {showCloseButton && (
              <button
                className="modal-panel__close"
                onClick={onClose}
                aria-label="Close modal"
              >
                ×
              </button>
            )}
          </div>
          <div className="modal-panel__content">{children}</div>
          {footer && <div className="modal-panel__footer">{footer}</div>}
        </div>
      </div>
    );
  }
);

ModalPanel.displayName = "ModalPanel";

export default ModalPanel;