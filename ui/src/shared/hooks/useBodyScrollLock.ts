import { useEffect } from "react";

/**
 * Hook that locks body scroll when active.
 * Uses the position:fixed pattern to preserve scroll position on iOS Safari.
 * Extracted from CameraFeedModal's inline implementation.
 *
 * @param isLocked - Whether body scroll should be locked
 */
export function useBodyScrollLock(isLocked: boolean): void {
  useEffect(() => {
    if (isLocked) {
      const scrollY = window.scrollY;
      document.body.style.overflow = "hidden";
      document.body.style.position = "fixed";
      document.body.style.top = `-${scrollY}px`;
      document.body.style.width = "100%";
      return () => {
        document.body.style.overflow = "";
        document.body.style.position = "";
        document.body.style.top = "";
        document.body.style.width = "";
        window.scrollTo(0, scrollY);
      };
    }
  }, [isLocked]);
}