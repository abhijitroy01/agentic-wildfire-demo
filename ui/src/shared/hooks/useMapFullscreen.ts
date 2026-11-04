import { useCallback, useEffect, useRef, useState } from "react";
import { useBodyScrollLock } from "./useBodyScrollLock";
import { useEscapeKey } from "./useEscapeKey";
import { useFocusTrap } from "./useFocusTrap";

/** Undock the existing map into the viewport without remounting it or losing replay state. */
export function useMapFullscreen(visible = true): {
  expanded: boolean; toggle: () => void; restore: () => void; containerRef: React.RefObject<HTMLDivElement>;
} {
  const [expanded, setExpanded] = useState(false);
  const containerRef = useFocusTrap<HTMLDivElement>({isActive: expanded,
    returnFocusOnDeactivate: false, initialFocus: "[data-map-fullscreen]"});
  const trigger = useRef<HTMLElement | null>(null);
  const restore = useCallback(() => {
    if (document.fullscreenElement === containerRef.current) {
      void document.exitFullscreen().catch(() => undefined);
    }
    setExpanded(false);
    requestAnimationFrame(() => trigger.current?.focus());
  }, [containerRef]);
  const toggle = useCallback(() => {
    if (expanded) restore();
    else {
      trigger.current = document.activeElement as HTMLElement | null;
      setExpanded(true);
      // Invoke inside the user's click so the browser can grant native fullscreen.
      // Viewport undocking still works in browsers without this API or when it is refused.
      if (containerRef.current?.requestFullscreen) {
        void containerRef.current.requestFullscreen({navigationUI: "hide"}).catch(() => undefined);
      }
    }
  }, [expanded, restore, containerRef]);
  useEscapeKey(restore, expanded);
  useBodyScrollLock(expanded);
  useEffect(() => {
    if (!expanded) return;
    const changed = (): void => { if (!document.fullscreenElement) restore(); };
    document.addEventListener("fullscreenchange", changed);
    return () => { document.removeEventListener("fullscreenchange", changed); };
  }, [expanded, restore]);
  useEffect(() => { if (!visible && expanded) restore(); }, [visible, expanded, restore]);
  return {expanded, toggle, restore, containerRef};
}
