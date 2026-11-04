/**
 * Type Guards for Runtime Type Checking
 *
 * Runtime checks and the closed value sets behind the firetruck display types.
 * Use these at system boundaries where external data enters the application.
 * This module imports nothing: the API image's ui-metrics stage copies it alone.
 */

// ============================================================================
// PRIMITIVE TYPE GUARDS
// ============================================================================

/**
 * Type guard for non-empty strings
 */
export function isNonEmptyString(value: unknown): value is string {
  return typeof value === "string" && value.length > 0;
}

// ============================================================================
// FIRETRUCK SIMULATOR VALUE SETS
// ============================================================================

/**
 * Valid unit types for fire trucks
 */
export const UNIT_TYPES = [
  "ENGINE",
  "TANKER",
  "BRUSH",
  "RESCUE",
  "COMMAND",
] as const;
export type UnitType = (typeof UNIT_TYPES)[number];

/**
 * Valid activity statuses for fire trucks
 */
export const ACTIVITY_STATUSES = [
  "EN_ROUTE",
  "SUPPRESSION",
  "HOLDING",
  "RESUPPLY",
  "STAGING",
  "EVACUATION",
] as const;
export type ActivityStatus = (typeof ACTIVITY_STATUSES)[number];

/**
 * Valid entrapment risk levels
 */
export const ENTRAPMENT_RISKS = ["LOW", "MEDIUM", "HIGH", "CRITICAL"] as const;
export type EntrapmentRisk = (typeof ENTRAPMENT_RISKS)[number];
