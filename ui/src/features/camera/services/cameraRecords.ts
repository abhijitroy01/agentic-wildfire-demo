/**
 * Camera records: parsing of Delphire's latest-records shape (alias groups of records) for the broker camera
 * client. It reads no camera Vite setting.
 *
 * One /latest answer provides everything the camera modal shows: the camera group's image record with its claims,
 * and the device-level sensors (GPS, wind, gas, air quality).
 */

import { MPS_TO_MPH } from "@/shared/domain/incidentObservations";
import { logger } from "@/shared/utils/logger";
import type {
  BoundingBox,
  CameraDetection,
  Claim,
  Detection,
  SensorGroup,
  SensorMetric,
} from "../types";

// ============================================================================
// SENSOR EXTRACTION - Declarative Configuration
// Maps API response aliases to UI-friendly sensor metrics
// ============================================================================

interface SensorExtractor {
  alias: string;
  extract: (
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    record: any,
  ) => Array<{
    key: string;
    icon: string;
    label: string;
    value: string | number | null;
    unit: string;
  }>;
}

/**
 * Declarative sensor extractors - add new sensors here
 * Each extractor knows how to pull data from its sensor group
 */
const SENSOR_EXTRACTORS: SensorExtractor[] = [
  {
    alias: "navigation",
    extract: (record) => {
      const gps = record?.gps;
      if (!gps?.latitude || !gps?.longitude) return [];
      return [
        {
          key: "gps",
          icon: "📍",
          label: "GPS",
          value: `${gps.latitude.toFixed(4)}, ${gps.longitude.toFixed(4)}`,
          unit: "",
        },
      ];
    },
  },
  {
    alias: "wind",
    extract: (record) => {
      const anemometer = record?.anemometer;
      if (!anemometer) return [];
      const metrics: SensorMetric[] = [];

      if (anemometer.speed != null) {
        const direction =
          anemometer.angle != null ? ` @ ${Math.round(anemometer.angle)}°` : "";
        // Delphire reports anemometer speed in m/s; the UI shows U.S. units.
        metrics.push({
          key: "wind",
          icon: "💨",
          label: "Wind",
          value: `${(anemometer.speed * MPS_TO_MPH).toFixed(1)}${direction}`,
          unit: "mph",
        });
      }
      return metrics;
    },
  },
  {
    alias: "gas",
    extract: (record) => {
      const gas = record?.gas;
      if (!gas) return [];
      const metrics: SensorMetric[] = [];

      if (gas.humidity != null) {
        metrics.push({
          key: "humidity",
          icon: "💧",
          label: "Humidity",
          value: gas.humidity.toFixed(1),
          unit: "%",
        });
      }
      if (gas.pressure != null) {
        metrics.push({
          key: "pressure",
          icon: "🌡️",
          label: "Pressure",
          value: gas.pressure.toFixed(1),
          unit: "hPa",
        });
      }
      return metrics;
    },
  },
  {
    alias: "air_quality",
    extract: (record) => {
      const voc = record?.voc;
      if (!voc) return [];
      const metrics: SensorMetric[] = [];

      if (voc.TVOC != null) {
        metrics.push({
          key: "tvoc",
          icon: "☁️",
          label: "TVOC",
          value: voc.TVOC.toFixed(1),
          unit: "ppb",
        });
      }
      if (voc.CO2 != null) {
        metrics.push({
          key: "co2",
          icon: "🏭",
          label: "CO₂",
          value: voc.CO2,
          unit: "ppm",
        });
      }
      return metrics;
    },
  },
];

// ============================================================================
// SENSOR EXTRACTION FROM /latest RESPONSE
// ============================================================================

/**
 * Extract the device-level sensor metrics (GPS, wind, gas, air quality) from a /latest answer.
 * All sensors are at device level, not per-camera.
 */
export function extractSensorsFromResponse(data: readonly SensorGroup[]): SensorMetric[] {
  const metrics: SensorMetric[] = [];
  for (const extractor of SENSOR_EXTRACTORS) {
    const record = data.find((g) => g.alias === extractor.alias)?.records?.[0];
    if (record) metrics.push(...extractor.extract(record));
  }
  logger.debug("[CameraRecords] Extracted sensors:", metrics.length, "metrics from /latest");
  return metrics;
}

// ============================================================================
// CAMERA DETECTION EXTRACTION
// ============================================================================

/** One class a detection claim names, with its confidence and, when the claim has one per class, its box. */
interface DetectionEntry {
  className: string;
  confidence: number;
  box: BoundingBox | null;
}

function isProbability(value: unknown): value is number {
  return typeof value === "number" && Number.isFinite(value) && value >= 0 && value <= 1;
}

/**
 * The entries of a valid detection claim: nonempty class names with one 0-1 confidence each. Boxes stay only when
 * every box is four finite numbers and there is one per class. An invalid claim gives no entries.
 */
function detectionEntries(claim: Claim | null | undefined): DetectionEntry[] {
  const detection = claim?.type === "detection" ? claim.detection : undefined;
  if (!detection) return [];
  const { classes, confidences, boundingBoxes } = detection;
  if (!Array.isArray(classes) || !Array.isArray(confidences) || classes.length === 0 ||
      classes.length !== confidences.length ||
      !classes.every((name) => typeof name === "string" && name.trim() !== "") ||
      !confidences.every(isProbability)) {
    return [];
  }
  const boxes = Array.isArray(boundingBoxes) && boundingBoxes.length === classes.length &&
    boundingBoxes.every((box) => Array.isArray(box) && box.length === 4 &&
      box.every((value) => typeof value === "number" && Number.isFinite(value)))
    ? boundingBoxes.map(([x, y, width, height]): BoundingBox => ({ x, y, width, height }))
    : null;
  return classes.map((className, index) => ({
    className, confidence: confidences[index], box: boxes ? boxes[index] : null,
  }));
}

/**
 * Smoke and fire probabilities (0-1) of an image record's claims. A valid dedicated smoke.smokeProb or
 * fire.fireProb claim wins (the last one); otherwise the highest confidence any valid detection claim gives the
 * class "smoke" or "fire" (trimmed, any case); otherwise 0, since Delphire attaches detection claims only for
 * detections.
 */
export function detectionProbabilities(claims: readonly Claim[] | null | undefined): {
  smokeProb: number;
  fireProb: number;
} {
  let smoke: number | null = null;
  let fire: number | null = null;
  const detected = { smoke: 0, fire: 0 };
  for (const claim of claims ?? []) {
    const smokeProb = claim?.type === "smoke" ? claim.smoke?.smokeProb : undefined;
    const fireProb = claim?.type === "fire" ? claim.fire?.fireProb : undefined;
    if (isProbability(smokeProb)) smoke = smokeProb;
    if (isProbability(fireProb)) fire = fireProb;
    for (const entry of detectionEntries(claim)) {
      const name = entry.className.trim().toLowerCase();
      if (name === "smoke" || name === "fire") detected[name] = Math.max(detected[name], entry.confidence);
    }
  }
  return { smokeProb: smoke ?? detected.smoke, fireProb: fire ?? detected.fire };
}

/**
 * Every valid detection claim's entries as one Detection. Entries with a box come first, so boundingBoxes[i]
 * belongs to classes[i], as CameraImageViewer labels boxes by index.
 */
function mergedDetection(entries: readonly DetectionEntry[]): Detection | null {
  if (!entries.length) return null;
  const ordered = [...entries.filter((entry) => entry.box), ...entries.filter((entry) => !entry.box)];
  return {
    classes: ordered.map((entry) => entry.className),
    confidences: ordered.map((entry) => entry.confidence),
    boundingBoxes: ordered.flatMap((entry) => (entry.box ? [entry.box] : [])),
  };
}

/**
 * Extract camera detection data from a /latest answer: the camera group's image record, its smoke and fire
 * probabilities and its detections.
 */
export function extractCameraDetection(
  data: readonly SensorGroup[],
  deviceId: string,
  targetSensorAlias: string,
): CameraDetection | null {
  // Find camera sensor group
  const cameraGroup = data.find(
    (g) => g.alias === targetSensorAlias || g.sensorAlias === targetSensorAlias,
  );

  // If target camera not found, try other camera groups
  const group =
    cameraGroup ||
    data.find((g) => g.alias?.startsWith("camera_") && g.records?.length > 0);

  if (!group?.records?.length) {
    logger.warn("[CameraRecords] No camera data found in /latest response");
    return null;
  }

  // Find first image record (not event)
  const record =
    group.records.find((r) => r.type === "image") || group.records[0];

  // An older answer shape carries the detection on the record itself.
  const claims: Claim[] = [...(record.claims ?? [])];
  if (!claims.some((claim) => claim?.type === "detection") && record.detection) {
    claims.push({ type: "detection", detection: record.detection });
  }

  return {
    ...detectionProbabilities(claims),
    detection: mergedDetection(claims.flatMap(detectionEntries)),
    evidenceId: record.id || null,
    timeRecorded: record.timeRecorded ?? "",
    sensorAlias: group.alias || group.sensorAlias || "unknown",
    deviceId,
  };
}
