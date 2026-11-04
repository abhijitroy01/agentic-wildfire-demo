/**
 * Camera through the workspace API broker (every published build): GET /simulation-api/v1/camera/latest and
 * /camera/images/{id} with this browser's demo session cookie. The broker alone holds the Delphire key, so the
 * browser sends no Authorization header and this module reads no camera Vite setting.
 */

import { anySignal, brokerFetch, brokerJson, initialize } from "@/shared/services/workspaceAccess";
import { logger } from "@/shared/utils/logger";
import type { CameraSnapshot, SensorGroup } from "../types";
import { extractCameraDetection, extractSensorsFromResponse } from "./cameraRecords";

/** GET /camera/latest: the broker's view of the configured device's latest records, camera group first. */
export type BrokerCameraLatest = {
  readonly device_id: string;
  readonly camera_alias: string;
  readonly groups: readonly SensorGroup[];
};

// The broker answers within two Delphire calls (latest, then an image) under CloudFront's 60-second origin
// timeout; bound every request at that.
export const CAMERA_REQUEST_TIMEOUT_MS = 60_000;
// Rounds of latest records, then the image they name: API processes keep their own latest records, so an image
// request can reach a process that does not hold the visitor's image ID yet, and a later round can find it.
export const CAMERA_ROUNDS = 3;
const IMAGE_TYPES = new Set(["image/webp", "image/png"]);

/** True when the workspace reports a configured camera; false when it does not, or the workspace is not open. */
export async function cameraAvailable(): Promise<boolean> {
  try {
    return (await initialize()).camera_configured === true;
  } catch {
    return false;
  }
}

function isLatest(value: unknown): value is BrokerCameraLatest {
  if (!value || typeof value !== "object") return false;
  const view = value as Record<string, unknown>;
  return typeof view.device_id === "string" && typeof view.camera_alias === "string" &&
    Array.isArray(view.groups) && view.groups.every((group: unknown) => {
      const item = group as Record<string, unknown> | null;
      return !!item && typeof item === "object" && typeof item.alias === "string" && Array.isArray(item.records);
    });
}

/**
 * Read the camera's latest records. Rejects with the server's message (for example, when the camera is not
 * configured), a readable timeout, or the caller's abort.
 */
export async function fetchBrokerLatest(signal?: AbortSignal): Promise<BrokerCameraLatest> {
  const value = await brokerJson("/camera/latest", {
    signal, timeoutMs: CAMERA_REQUEST_TIMEOUT_MS, timeoutMessage: "The camera service did not answer in time.",
  });
  if (!isLatest(value)) throw new Error("The camera returned an unusable response.");
  return value;
}

/**
 * Read one image the latest records name as a blob: URL (released with revokeImageUrl), or null when the broker
 * does not serve it. Only the caller's abort rejects. The body is an image, not JSON, so this is the one camera
 * call on brokerFetch, with the same bound.
 */
export async function fetchBrokerImage(id: string, signal?: AbortSignal): Promise<string | null> {
  const timeout = AbortSignal.timeout(CAMERA_REQUEST_TIMEOUT_MS);
  try {
    const response = await brokerFetch(`/camera/images/${encodeURIComponent(id)}`, {
      method: "GET", signal: signal ? anySignal([signal, timeout]) : timeout,
      headers: { Accept: "image/webp, image/png" },
    });
    const type = (response.headers.get("content-type") || "").split(";")[0].trim().toLowerCase();
    if (!IMAGE_TYPES.has(type)) {
      logger.debug("[CameraBroker] Camera image is not WebP or PNG:", type);
      return null;
    }
    return URL.createObjectURL(await response.blob());
  } catch (error) {
    if (signal?.aborted) throw error;
    logger.debug("[CameraBroker] Camera image unavailable:", error instanceof Error ? error.message : error);
    return null;
  }
}

/**
 * Revoke blob URL to free memory
 */
export function revokeImageUrl(url: string): void {
  if (url?.startsWith("blob:")) {
    URL.revokeObjectURL(url);
  }
}

/** The ID of the image record in the camera's group, the only image the broker serves. */
function imageIdOf(latest: BrokerCameraLatest): string | null {
  const group = latest.groups.find((item) => item.alias === latest.camera_alias);
  return group?.records.find((record) => record.type === "image")?.id || null;
}

/**
 * The camera modal's snapshot: up to CAMERA_ROUNDS rounds of latest records, then the image that answer names.
 * The detections and the image always come from the same answer. A latest-records error ends the rounds.
 */
export async function brokerCameraSnapshot(signal?: AbortSignal): Promise<CameraSnapshot> {
  let latest = await fetchBrokerLatest(signal);
  let imageUrl: string | null = null;
  for (let round = 1; ; round += 1) {
    const id = imageIdOf(latest);
    imageUrl = id ? await fetchBrokerImage(id, signal) : null;
    if (imageUrl || !id || round >= CAMERA_ROUNDS) break;
    latest = await fetchBrokerLatest(signal);
  }
  const detection = extractCameraDetection(latest.groups, latest.device_id, latest.camera_alias);
  return {
    detection,
    imageUrl,
    sensors: extractSensorsFromResponse(latest.groups),
    timestamp: detection?.timeRecorded || new Date().toISOString(),
    deviceId: latest.device_id,
    sensorAlias: latest.camera_alias,
  };
}
