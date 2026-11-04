import { useEscapeKey } from "@/shared/hooks/useEscapeKey";
import type { DockedPanelProps } from "@/shared/components/panels/types";
import WorkspaceHeading from "./WorkspaceHeading";
import { useCallback, useEffect, useRef } from "react";

/** Full workspace surface for the standalone exercise, opened from 3 · Response Tracker; closing returns there. */
export default function WorkspacePanel({ isOpen, onClose, title, children, className }: DockedPanelProps): React.ReactElement | null {
  const surface = useRef<HTMLElement>(null);
  const close = useRef<HTMLButtonElement>(null);
  const escape = useCallback(() => {
    if (!surface.current?.querySelector(".map-undocked")) onClose();
  }, [onClose]);
  useEscapeKey(escape, isOpen);
  useEffect(() => { if (isOpen) close.current?.focus(); }, [isOpen]);
  if (!isOpen) return null;
  return <main ref={surface} className={`workspace-surface response-workspace ${className || ""}`} aria-label={title}>
    <WorkspaceHeading eyebrow="STANDALONE EXERCISE" title={title}
      description="A separate synthetic suppression exercise. It is not linked to the run or policy selected in 2 · Evacuation & Crew Access." />
    <button ref={close} className="secondary workspace-close" onClick={onClose}>Return to Response Tracker</button>
    <div className="response-workspace-content">{children}</div>
  </main>;
}
