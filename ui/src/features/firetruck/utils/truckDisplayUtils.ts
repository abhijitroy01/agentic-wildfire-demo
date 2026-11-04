/**
 * Firetruck display vocabulary: the one TruckView the exercise card, map overlay and legend show, and the unit,
 * status and risk tables they render from. Runtime dependency-free: the ui-metrics image stage copies this file.
 */
import type { ActivityStatus,EntrapmentRisk,UnitType } from "@/shared/utils/typeGuards";

/** One exercise unit as the dashboard card, the map overlay and its popup show it. */
export interface TruckView {
  id: string;
  unitType: string;
  latitude: number;
  longitude: number;
  heading: number;
  speedMph: number;
  status: string;
  waterLevelPct: number;
  pumpActive: boolean;
  flowRateGpm: number;
  crewCount: number;
  hoursOnDuty: number;
  inDangerZone: boolean;
  entrapmentRisk: string;
  position: { latitude: number; longitude: number };
}

/** Icon and name for each unit type, in legend order. */
export const UNIT_DISPLAY: Readonly<Record<UnitType, { readonly icon: string; readonly name: string }>> = {
  ENGINE: { icon: "🚒", name: "Engine" },
  TANKER: { icon: "🚛", name: "Tanker" },
  RESCUE: { icon: "🚑", name: "Rescue" },
  BRUSH: { icon: "🚙", name: "Brush" },
  COMMAND: { icon: "🚗", name: "Command" },
};

/** Marker ring color and legend label for each activity status, in legend order. */
export const STATUS_DISPLAY: Readonly<Record<ActivityStatus, { readonly color: string; readonly label: string }>> = {
  EN_ROUTE: { color: "#3B82F6", label: "En Route" },
  SUPPRESSION: { color: "#EF4444", label: "Suppression" },
  RESUPPLY: { color: "#8B5CF6", label: "Resupply" },
  HOLDING: { color: "#F59E0B", label: "Holding" },
  STAGING: { color: "#6B7280", label: "Staging" },
  EVACUATION: { color: "#DC2626", label: "Evacuation" },
};

/** Badge and popup color for each entrapment risk level. */
export const RISK_DISPLAY: Readonly<Record<EntrapmentRisk, string>> = {
  LOW: "#10B981",
  MEDIUM: "#F59E0B",
  HIGH: "#EF4444",
  CRITICAL: "#DC2626",
};

/** Gray for a status or risk value outside the tables. */
const UNKNOWN_COLOR = "#6B7280";

/** The table entry for a value in any letter case, or undefined for a value the table does not define. */
function lookup<K extends string, V>(table: Readonly<Record<K, V>>, value: string | undefined): V | undefined {
  const key = value?.toUpperCase();
  return key && key in table ? table[key as K] : undefined;
}

/**
 * Get icon for unit type
 */
export function getUnitIcon(unitType?: string): string {
  return lookup(UNIT_DISPLAY, unitType)?.icon ?? UNIT_DISPLAY.ENGINE.icon;
}

/**
 * Get the display name for a unit type, for example "ENGINE" → "Engine"
 */
export function getUnitName(unitType: string): string {
  return lookup(UNIT_DISPLAY, unitType)?.name ?? unitType;
}

/**
 * Get color based on truck activity status
 */
export function getStatusColor(status?: string): string {
  return lookup(STATUS_DISPLAY, status)?.color ?? UNKNOWN_COLOR;
}

/**
 * Get color based on entrapment risk level
 */
export function getRiskColor(risk?: string): string {
  return lookup(RISK_DISPLAY, risk) ?? UNKNOWN_COLOR;
}

/**
 * Format status string for display (replace underscores with spaces)
 */
export function formatStatus(status?: string): string {
  if (!status) return "UNKNOWN";
  return status.replace(/_/g, " ");
}

/**
 * Get water level bar color based on percentage
 */
export function getWaterLevelColor(waterLevelPct: number): string {
  if (waterLevelPct < 20) return "#EF4444"; // Red - critical
  if (waterLevelPct < 50) return "#F59E0B"; // Amber - low
  return "#3B82F6"; // Blue - normal
}

/**
 * Format elapsed seconds as MM:SS
 */
export function formatElapsedTime(seconds: number): string {
  const mins = Math.floor(seconds / 60);
  const secs = seconds % 60;
  return `${mins}:${secs.toString().padStart(2, "0")}`;
}
