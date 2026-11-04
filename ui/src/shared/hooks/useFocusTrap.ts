/**
 * Focus Trap Hook for Accessibility
 *
 * Traps keyboard focus within a container element when active.
 * Essential for modal dialogs, slide-out panels, and other overlays.
 *
 * WCAG 2.1 Level AA requires:
 * - Focus must be trapped within modal dialogs
 * - Focus must return to trigger element when modal closes
 * - Tab and Shift+Tab should cycle through focusable elements
 *
 * @example
 * function Modal({ isOpen, onClose }) {
 *   const containerRef = useFocusTrap<HTMLDivElement>({
 *     isActive: isOpen,
 *     returnFocusOnDeactivate: true
 *   });
 *
 *   return (
 *     <div ref={containerRef} role="dialog" aria-modal="true">
 *       <button onClick={onClose}>Close</button>
 *       <input type="text" />
 *     </div>
 *   );
 * }
 */

import { useCallback,useEffect,useRef } from "react";

// ============================================================================
// TYPES
// ============================================================================

export interface UseFocusTrapOptions {
  /** Whether the focus trap is currently active */
  isActive: boolean;

  /** Return focus to previously focused element when trap deactivates */
  returnFocusOnDeactivate?: boolean;

  /** Callback when user attempts to tab out of trap (for custom behavior) */
  onEscapeAttempt?: () => void;

  /** Auto-focus first focusable element when trap activates */
  autoFocus?: boolean;

  /** Initial element to focus (selector or element) */
  initialFocus?: string | HTMLElement;
}

// ============================================================================
// CONSTANTS
// ============================================================================

/** Selector for all focusable elements */
const FOCUSABLE_ELEMENTS_SELECTOR = [
  "button:not([disabled])",
  "[href]",
  "input:not([disabled])",
  "select:not([disabled])",
  "textarea:not([disabled])",
  '[tabindex]:not([tabindex="-1"])',
  '[contenteditable="true"]',
].join(", ");

// ============================================================================
// HOOK
// ============================================================================

/**
 * Hook that traps focus within a container element.
 *
 * @param options Configuration options for the focus trap
 * @returns Ref to attach to the container element
 */
export function useFocusTrap<T extends HTMLElement>(
  options: UseFocusTrapOptions
): React.RefObject<T> {
  const {
    isActive,
    returnFocusOnDeactivate = true,
    onEscapeAttempt,
    autoFocus = true,
    initialFocus,
  } = options;

  const containerRef = useRef<T>(null);
  const previousActiveElement = useRef<Element | null>(null);

  /**
   * Get all focusable elements within the container
   */
  const getFocusableElements = useCallback((): HTMLElement[] => {
    if (!containerRef.current) return [];

    const elements = containerRef.current.querySelectorAll(
      FOCUSABLE_ELEMENTS_SELECTOR
    );

    // Filter to only visible elements
    return Array.from(elements).filter((el) => {
      const htmlEl = el as HTMLElement;
      return (
        htmlEl.offsetParent !== null && // Not hidden via display: none
        !htmlEl.hasAttribute("aria-hidden") &&
        htmlEl.getAttribute("aria-disabled") !== "true"
      );
    }) as HTMLElement[];
  }, []);

  /**
   * Focus the first focusable element in the container
   */
  const focusFirstElement = useCallback(() => {
    if (!containerRef.current) return;

    // Check for initial focus target
    if (initialFocus) {
      let targetElement: HTMLElement | null = null;

      if (typeof initialFocus === "string") {
        targetElement = containerRef.current.querySelector(initialFocus);
      } else {
        targetElement = initialFocus;
      }

      if (targetElement) {
        targetElement.focus();
        return;
      }
    }

    // Fall back to first focusable element
    const focusableElements = getFocusableElements();
    if (focusableElements.length > 0) {
      focusableElements[0].focus();
    }
  }, [getFocusableElements, initialFocus]);

  /**
   * Handle keyboard navigation within the trap
   */
  const handleKeyDown = useCallback(
    (event: KeyboardEvent) => {
      if (!isActive || !containerRef.current) return;

      // Handle Escape key
      if (event.key === "Escape" && onEscapeAttempt) {
        onEscapeAttempt();
        return;
      }

      // Only trap Tab key
      if (event.key !== "Tab") return;

      const focusableElements = getFocusableElements();
      if (focusableElements.length === 0) return;

      const firstElement = focusableElements[0];
      const lastElement = focusableElements[focusableElements.length - 1];

      // Shift + Tab on first element -> go to last
      if (event.shiftKey && document.activeElement === firstElement) {
        event.preventDefault();
        lastElement.focus();
        return;
      }

      // Tab on last element -> go to first
      if (!event.shiftKey && document.activeElement === lastElement) {
        event.preventDefault();
        firstElement.focus();
        return;
      }

      // Ensure focus stays within container
      if (!containerRef.current.contains(document.activeElement)) {
        event.preventDefault();
        firstElement.focus();
      }
    },
    [isActive, getFocusableElements, onEscapeAttempt]
  );

  /**
   * Effect: Set up and tear down focus trap
   */
  useEffect(() => {
    if (!isActive || !containerRef.current) return;

    // Store previously focused element
    previousActiveElement.current = document.activeElement;

    // Auto-focus first element
    if (autoFocus) {
      // Small delay to ensure container is rendered
      requestAnimationFrame(() => {
        focusFirstElement();
      });
    }

    // Add keyboard event listener
    document.addEventListener("keydown", handleKeyDown);

    return () => {
      document.removeEventListener("keydown", handleKeyDown);

      // Return focus to previous element
      if (
        returnFocusOnDeactivate &&
        previousActiveElement.current &&
        previousActiveElement.current instanceof HTMLElement
      ) {
        // Small delay to prevent focus flash
        requestAnimationFrame(() => {
          (previousActiveElement.current as HTMLElement)?.focus?.();
        });
      }
    };
  }, [
    isActive,
    autoFocus,
    returnFocusOnDeactivate,
    handleKeyDown,
    focusFirstElement,
  ]);

  return containerRef;
}

export default useFocusTrap;
