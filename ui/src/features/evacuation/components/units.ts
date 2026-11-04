// API artifacts and SUMO retain their original SI units.
export const MILES_PER_KM = 1 / 1.609344;
export const FEET_PER_METER = 1 / 0.3048;
const nonnegative = (value: unknown): value is number => typeof value === "number" && Number.isFinite(value) && value >= 0;
/** A count or measure in U.S. format with at most one decimal, for example 1,234.5. */
export const formatNumber = (n: number) : string => n.toLocaleString("en-US", { maximumFractionDigits: 1 });
/** An epoch-seconds or ISO time as a U.S. date and time with its zone; "Unavailable" when absent or invalid. */
export const formatDateTime = (value: unknown) : string => {
  if ((typeof value !== "number" && typeof value !== "string") || value === "") return "Unavailable";
  const time = new Date(typeof value === "number" ? value * 1000 : value);
  return Number.isFinite(time.getTime()) ? time.toLocaleString("en-US", {
    month: "short", day: "numeric", year: "numeric", hour: "numeric", minute: "2-digit", timeZoneName: "short",
  }) : "Unavailable";
};
export const mph = (kmh: number | null | undefined) : string => nonnegative(kmh) ? `${(kmh * MILES_PER_KM).toFixed(1)} mph` : "Unavailable";
export const speedMph = (metersPerSecond: number | null | undefined) : string =>
  nonnegative(metersPerSecond) ? mph(metersPerSecond * 3.6) : "Unavailable";
export const miles = (km: number | null | undefined) : string => nonnegative(km) ? `${(km * MILES_PER_KM).toFixed(2)} mi` : "Unavailable";
export const feet = (meters: number | null | undefined) : string =>
  nonnegative(meters) ? `${formatNumber(meters * FEET_PER_METER)} ft` : "Unavailable";
export const temperatureF = (value: number | null | undefined, unit: string) : string =>
  typeof value !== "number" || !Number.isFinite(value) ? "Temperature unavailable"
    : unit === "C" ? `${Math.round(value * 9 / 5 + 32)}°F` : unit === "F" ? `${value}°F` : "Temperature unavailable";

export function fromUSInput(text: string, factor: number, min: number, max: number): number | "" {
  if (text === "") return "";
  const value = Number(text) / factor;
  // Displayed boundary values are rounded to two decimals; preserve the API bounds.
  const tolerance = 0.0050001 / factor;
  if (Math.abs(value - min) <= tolerance) return min;
  if (Math.abs(value - max) <= tolerance) return max;
  return value;
}
