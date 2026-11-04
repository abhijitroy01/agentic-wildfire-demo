/**
 * useCameraData Hook
 * Custom hook for managing camera snapshot state and lifecycle
 *
 * Senior Architect Design Principles:
 * 1. Separate effect for activation vs mounted lifecycle
 * 2. Blob URL cleanup is deterministic (no closures in cleanup)
 * 3. State clears immediately on deactivation to prevent stale renders
 * 4. AbortController for proper async cancellation
 */

import { logger } from "@/shared/utils/logger";
import { useCallback,useEffect,useRef,useState } from "react";
import { brokerCameraSnapshot,revokeImageUrl } from "../services/cameraBroker";
import type { CameraSnapshot } from "../types";

/**
 * Return type for useCameraData hook
 * Clean, predictable interface for consuming components
 */
export interface UseCameraDataReturn {
  // Data
  snapshot: CameraSnapshot | null;

  // Loading/Error states
  isLoading: boolean;
  error: string | null;

  // Actions
  refresh: () => Promise<void>;

  // Config (for display)
  config: {
    deviceId: string;
    sensorAlias: string;
  };
}

/**
 * Helper: Safely revoke blob URL from ref
 * Encapsulates the revocation pattern for reuse
 */
function safeRevokeBlobUrl(ref: React.MutableRefObject<string | null>): void {
  if (ref.current) {
    revokeImageUrl(ref.current);
    ref.current = null;
  }
}

/**
 * Custom hook for camera data management
 *
 * Features:
 * - Automatic fetch when isActive becomes true
 * - Automatic cleanup of blob URLs on unmount
 * - Prevents stale data issues with mounted ref
 * - AbortController for proper async cancellation
 * - Single entry point for all camera data
 *
 * @param isActive - Whether to fetch data (typically tied to modal open state)
 */
export function useCameraData(isActive: boolean): UseCameraDataReturn {
  const [snapshot, setSnapshot] = useState<CameraSnapshot | null>(null);
  const [isLoading, setIsLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Refs: source of truth for cleanup (not state)
  const imageUrlRef = useRef<string | null>(null);
  const isMountedRef = useRef(true);
  const activeRef = useRef(isActive);
  activeRef.current = isActive;
  const requestRef = useRef<AbortController | null>(null);

  // The broker's snapshot names its device and camera; before it loads there is none to show.
  const config = snapshot
    ? { deviceId: snapshot.deviceId, sensorAlias: snapshot.sensorAlias }
    : { deviceId: "", sensorAlias: "" };

  /**
   * Fetch camera data with abort signal support
   * Internal function that accepts an AbortSignal for cancellation
   */
  const fetchCameraData = useCallback(
    async (signal: AbortSignal): Promise<void> => {
      if (!isMountedRef.current) return;

      setIsLoading(true);
      setError(null);

      try {
        const data = await brokerCameraSnapshot(signal);

        // Check abort status before updating state
        if (signal.aborted || !isMountedRef.current) {
          // Cleanup blob URL if fetch completed but we're aborting
          if (data.imageUrl) {
            revokeImageUrl(data.imageUrl);
          }
          return;
        }

        // Revoke OLD blob URL BEFORE storing new one
        safeRevokeBlobUrl(imageUrlRef);

        // Store new URL in ref (source of truth for cleanup)
        imageUrlRef.current = data.imageUrl;

        setSnapshot(data);

        // Check if we got detection data
        if (!data.detection) {
          setError("No detection data available from camera");
        }
      } catch (err) {
        // Don't set error if aborted - this is expected behavior
        if (err instanceof DOMException && err.name === "AbortError") {
          return;
        }
        if (signal.aborted) return;
        if (!isMountedRef.current) return;

        logger.error("[useCameraData] Fetch failed:", err);
        setError(
          err instanceof Error
            ? err.message
            : "Failed to connect to camera. Please try again.",
        );
      } finally {
        if (!signal.aborted && isMountedRef.current) {
          setIsLoading(false);
        }
      }
    },
    [],
  );

  /**
   * Manual refresh - creates its own AbortController
   * For user-triggered refreshes (e.g., refresh button)
   */
  const refresh = useCallback(async (): Promise<void> => {
    if (!activeRef.current || !isMountedRef.current) return;
    requestRef.current?.abort();
    const controller = new AbortController();
    requestRef.current = controller;
    await fetchCameraData(controller.signal);
  }, [fetchCameraData]);

  /**
   * Effect 1: Handle activation state changes
   *
   * Principle #1: Separate effect for activation
   * Principle #2: Deterministic cleanup (no closures)
   * Principle #3: State clears immediately on deactivation
   * Principle #4: AbortController for proper cancellation
   */
  useEffect(() => {
    const abortController = new AbortController();
    requestRef.current?.abort();
    requestRef.current = abortController;

    if (isActive) {
      // Modal opened - fetch fresh data
      fetchCameraData(abortController.signal);
    } else {
      // Modal closed - clear state immediately (Principle #3)
      // This prevents stale blob URLs from being rendered
      setSnapshot(null);
      setError(null);
      setIsLoading(false);
    }

    // Cleanup: runs on every re-run or unmount
    // Principle #2: Deterministic - no conditional on captured isActive
    return () => {
      abortController.abort();
      requestRef.current?.abort();
      safeRevokeBlobUrl(imageUrlRef);
    };
  }, [isActive, fetchCameraData]);

  /**
   * Effect 2: Track mounted state only
   *
   * Principle #1: Separate effect (single responsibility)
   * This effect ONLY manages the isMountedRef lifecycle
   */
  useEffect(() => {
    isMountedRef.current = true;
    return () => {
      isMountedRef.current = false;
    };
  }, []);

  return {
    snapshot,
    isLoading,
    error,
    refresh,
    config: {
      deviceId: config.deviceId,
      sensorAlias: config.sensorAlias,
    },
  };
}

export default useCameraData;
