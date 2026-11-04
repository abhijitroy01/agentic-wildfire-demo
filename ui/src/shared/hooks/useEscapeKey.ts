import { useEffect } from "react";

/**
 * Hook that calls a callback when the Escape key is pressed.
 * Extracted from CameraFeedModal's inline implementation.
 *
 * @param onEscape - Callback to invoke when Escape is pressed
 * @param isActive - Whether the hook is active (default: true)
 */
export function useEscapeKey(
  onEscape: () => void,
  isActive: boolean = true
): void {
  useEffect(() => {
    if (!isActive) return;
    const handler = (event: KeyboardEvent) : void => {
      if (event.key === "Escape") onEscape();
    };
    document.addEventListener("keydown", handler);
    return () => document.removeEventListener("keydown", handler);
  }, [onEscape, isActive]);
}