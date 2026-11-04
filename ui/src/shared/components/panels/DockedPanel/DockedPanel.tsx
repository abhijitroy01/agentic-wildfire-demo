import { useEscapeKey } from "@/shared/hooks/useEscapeKey";
import type React from "react";
import { memo,useEffect,useRef } from "react";
import type { DockedPanelProps } from "../types";
import { usePanelManager } from "../usePanelManager";
import "./DockedPanel.css";

const DockedPanel: React.FC<DockedPanelProps> = memo(
  ({
    id,
    isOpen,
    onClose,
    title,
    icon,
    children,
    className,
    position = "right",
    width = 420,
    topOffset = 80,
    zIndex: zIndexOverride,
    edgeOffset: edgeOffsetOverride,
    "aria-label": ariaLabel,
  }) => {
    const panelRef = useRef<HTMLElement>(null);
    const { registerPanel, unregisterPanel, getZIndex, getEdgeOffset } =
      usePanelManager();

    // Register with PanelProvider on mount
    useEffect(() => {
      registerPanel({
        id,
        type: "docked",
        position,
        width: typeof width === "number" ? width : 420,
      });
      return () => unregisterPanel(id);
      // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [id, position, width]);

    // ESC to close
    useEscapeKey(onClose, isOpen);

    // Focus management: move focus into panel on open
    useEffect(() => {
      if (isOpen && panelRef.current) {
        const firstFocusable = panelRef.current.querySelector<HTMLElement>(
          'button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])'
        );
        firstFocusable?.focus();
      }
    }, [isOpen]);

    if (!isOpen) return null;

    const computedZIndex = zIndexOverride ?? getZIndex(id);
    const computedOffset = edgeOffsetOverride ?? getEdgeOffset(id);

    return (
      <aside
        ref={panelRef}
        className={`docked-panel docked-panel--${position} ${className || ""}`}
        role="complementary"
        aria-label={ariaLabel || title}
        style={{
          [position]: computedOffset,
          top: topOffset,
          width: typeof width === "number" ? `${width}px` : width,
          zIndex: computedZIndex,
        }}
      >
        <div className="docked-panel__header">
          <div className="docked-panel__title-row">
            {icon && (
              <span className="docked-panel__icon" aria-hidden="true">
                {icon}
              </span>
            )}
            <h3 className="docked-panel__title">{title}</h3>
          </div>
          <button
            className="docked-panel__close"
            onClick={onClose}
            aria-label="Close panel"
          >
            ✕
          </button>
        </div>
        <div className="docked-panel__content">{children}</div>
      </aside>
    );
  }
);

DockedPanel.displayName = "DockedPanel";

export default DockedPanel;