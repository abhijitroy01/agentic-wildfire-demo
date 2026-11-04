import type { ReactNode } from "react";

// ============================================================================
// BASE TYPES
// ============================================================================

export interface BasePanelProps {
  id: string;
  isOpen: boolean;
  onClose: () => void;
  title: string;
  icon?: ReactNode;
  children: ReactNode;
  className?: string;
  "aria-label"?: string;
}

// ============================================================================
// DOCKED PANEL
// ============================================================================

export interface DockedPanelProps extends BasePanelProps {
  position?: "right" | "left";
  width?: number | string;
  topOffset?: number;
  zIndex?: number;
  edgeOffset?: number;
}

// ============================================================================
// MODAL PANEL
// ============================================================================

export interface ModalPanelProps extends BasePanelProps {
  maxWidth?: string;
  closeOnBackdropClick?: boolean;
  closeOnEscape?: boolean;
  showCloseButton?: boolean;
  footer?: ReactNode;
}

// ============================================================================
// FLOATING ACTION BUTTON
// ============================================================================

export interface FloatingActionButtonProps {
  onClick: () => void;
  icon: ReactNode;
  badge?: string | number;
  /** The only placement FloatingActionButton.css styles. */
  position?: "bottom-right";
  "aria-label": string;
  className?: string;
  pulseBadge?: boolean;
}

// ============================================================================
// PANEL PROVIDER
// ============================================================================

export interface PanelRegistration {
  id: string;
  type: "docked" | "modal";
  position?: "right" | "left";
  width?: number;
}

export interface PanelManagerContextValue {
  openPanel: (id: string) => void;
  closePanel: (id: string) => void;
  isPanelOpen: (id: string) => boolean;
  getZIndex: (id: string) => number;
  getEdgeOffset: (id: string) => number;
  registerPanel: (registration: PanelRegistration) => void;
  unregisterPanel: (id: string) => void;
  getOpenPanels: () => string[];
}