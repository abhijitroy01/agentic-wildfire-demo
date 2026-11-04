/**
 * Popup Generators
 * Clean, inline-styled popup generation for ArcGIS map features
 * Uses inline styles for reliable rendering in ArcGIS popup containers
 * Uses ArcGIS PopupTemplate actions for interactive buttons (proper approach)
 */

import type { FireData,WeatherData } from "@/features/map/types/types";
import { observationValue } from "@/shared/domain/incidentObservations";

/** Escape HTML special characters to prevent XSS in popup template strings (OWASP Rule #1) */
export function escapeHtml(unsafe: string | number | null | undefined): string {
  if (unsafe === null || unsafe === undefined) return '';
  return String(unsafe)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;')
    .replace(/>/g, '&gt;').replace(/"/g, '&quot;').replace(/'/g, '&#039;');
}

export interface PopupResult {
  title: string;
  content: string;
}

/**
 * Camera feed action definition - used for fire popups
 * This is the ArcGIS-native way to add an interactive button
 */
export const CAMERA_FEED_ACTION = {
  title: "📷 View Camera Feed",
  id: "view-camera-feed",
  className: "esri-icon-media", // ArcGIS built-in icon
};

/**
 * The popup actions for a fire or weather marker: the camera action only while a camera is available
 * (the workspace's camera_configured in broker mode). Every camera action is built here.
 */
export function cameraPopupActions(available: boolean): Array<typeof CAMERA_FEED_ACTION> {
  return available ? [CAMERA_FEED_ACTION] : [];
}

// Inline style constants for consistent, ArcGIS-compatible styling
const STYLES = {
  container:
    'font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; max-width: 320px;',
  header:
    "padding: 12px; border-radius: 8px 8px 0 0; margin: -10px -10px 12px -10px; color: white;",
  headerFire: "background: linear-gradient(135deg, #DC2626, #B91C1C);",
  headerLocation: "background: linear-gradient(135deg, #3B82F6, #1D4ED8);",
  headerDemographic: "background: linear-gradient(135deg, #1E40AF, #1E3A8A);",
  headerVegetation: "background: linear-gradient(135deg, #16A34A, #15803D);",
  title: "margin: 0; font-size: 16px; font-weight: 700;",
  content: "background: #F8FAFC; border-radius: 8px; padding: 12px;",
  cameraButton:
    "display: block; width: 100%; margin-top: 12px; padding: 10px 16px; background: linear-gradient(135deg, #6366F1, #4F46E5); color: white; border: none; border-radius: 6px; font-size: 13px; font-weight: 600; cursor: pointer; text-align: center;",
  metricsGrid: "display: flex; gap: 8px; margin-bottom: 12px;",
  metric: "flex: 1; border-radius: 8px; padding: 10px; text-align: center;",
  metricFire: "background: #FEF2F2;",
  metricSuccess: "background: #F0FDF4;",
  metricNeutral: "background: #F8FAFC;",
  metricValue: "font-size: 18px; font-weight: 700; margin-bottom: 4px;",
  metricValueFire: "color: #DC2626;",
  metricValueSuccess: "color: #059669;",
  metricValueNeutral: "color: #6B7280;",
  metricLabel: "font-size: 11px; color: #6B7280;",
  row: "margin-bottom: 6px; font-size: 12px;",
  section:
    "border-top: 1px solid #E5E7EB; padding-top: 10px; margin-top: 10px;",
  sectionTitle:
    "margin: 0 0 8px 0; color: #1F2937; font-size: 14px; font-weight: 600;",
  grid2Col:
    "display: grid; grid-template-columns: 1fr 1fr; gap: 8px; margin-bottom: 8px;",
  card: "border-radius: 6px; padding: 8px;",
  cardBlue: "background: #EFF6FF;",
  cardRed: "background: #FEF2F2;",
  cardOrange: "background: #FFF7ED;",
  cardLabel: "font-size: 11px; font-weight: 600; margin-bottom: 4px;",
  cardValue: "font-size: 16px; font-weight: 700;",
  cardSubtext: "font-size: 10px; color: #64748B; margin-top: 2px;",
};

/**
 * Create popup for fire incident with optional weather data
 *
 * SCHEMA COMPLIANCE: Uses camelCase field names matching OpenAPI schema:
 * - acresBurned (not acres_burned)
 * - percentContained (not percent_contained)
 * - isActive (not is_active)
 * - fireCause (not fire_cause)
 * - startedDate (not started_date)
 */
export function createFirePopup(
  fire: FireData | null,
  weather?: WeatherData | null,
): PopupResult {
  if (!fire) return { title: "Fire", content: "No data available" };

  const fireName = escapeHtml(fire.name) || "Fire Incident";
  const containment = fire.percentContained;
  const acres = fire.acresBurned;
  const isActive = fire.isActive;

  // Build location string
  const locationParts = [fire.location, fire.county, fire.state].filter(
    Boolean,
  );
  const locationStr =
    locationParts.length > 0 ? escapeHtml(locationParts.join(", ")) : null;

  // Status metric
  const statusMetric =
    isActive !== undefined
      ? `<div style="${STYLES.metric} ${STYLES.metricNeutral}">
         <div style="${STYLES.metricValue} ${
           isActive ? STYLES.metricValueFire : STYLES.metricValueSuccess
         }">
           ${isActive ? "Active" : "Inactive"}
         </div>
         <div style="${STYLES.metricLabel}">Status</div>
       </div>`
      : "";

  const title = `🔥 ${fireName}`;
  const content = `
    <div style="${STYLES.container}">
      <div style="${STYLES.header} ${STYLES.headerFire}">
        <h3 style="${STYLES.title}">🔥 ${fireName}</h3>
      </div>
      <div style="${STYLES.content}">
        <div style="${STYLES.metricsGrid}">
          <div style="${STYLES.metric} ${STYLES.metricFire}">
            <div style="${STYLES.metricValue} ${
              STYLES.metricValueFire
            }">${acres == null ? "Unavailable" : acres.toLocaleString()}</div>
            <div style="${STYLES.metricLabel}">Acres</div>
          </div>
          <div style="${STYLES.metric} ${STYLES.metricSuccess}">
            <div style="${STYLES.metricValue} ${
              STYLES.metricValueSuccess
            }">${containment == null ? "Unavailable" : `${containment}%`}</div>
            <div style="${STYLES.metricLabel}">Contained</div>
          </div>
          ${statusMetric}
        </div>
        ${
          locationStr
            ? `<div style="${STYLES.row}"><strong>📍 Location:</strong> ${locationStr}</div>`
            : ""
        }
        ${
          fire.fireCause
            ? `<div style="${STYLES.row}"><strong>Cause:</strong> ${escapeHtml(fire.fireCause)}</div>`
            : ""
        }
        ${
          fire.startedDate
            ? `<div style="${STYLES.row}"><strong>Started:</strong> ${new Date(
                fire.startedDate,
              ).toLocaleDateString()}</div>`
            : ""
        }
        ${formatWeatherSection(weather)}
      </div>
    </div>
  `;

  // Note: Camera feed button is now added via ArcGIS popup actions (see CAMERA_FEED_ACTION)
  // This is the proper ArcGIS pattern instead of HTML buttons in popup content

  return { title, content };
}

/**
 * Format weather section with inline styles - all in 2-column grid
 */
function formatWeatherSection(weather: WeatherData | null | undefined): string {
  if (!weather) return "";
  const cards = Object.entries(weather).map(([key, value]) => `<div style="${STYLES.card}">
    <div style="${STYLES.cardLabel}">${escapeHtml(key.replace(/([A-Z])/g, " $1"))}</div>
    <div style="${STYLES.cardValue}">${escapeHtml(observationValue(value))}</div>
  </div>`);
  if (!cards.length) return "";
  return `<div style="${STYLES.section}"><h4 style="${STYLES.sectionTitle}">Current Weather</h4>
    <div style="${STYLES.grid2Col}">${cards.join("")}</div>
    <small>Reported observations; missing values are unavailable.</small></div>`;
}

/**
 * Create popup for demographic data
 */
export function createDemographicPopup(
  attributes: Record<string, unknown>,
): PopupResult {
  const title = `📊 ${escapeHtml(attributes.name as string) || "Population Area"}`;
  const content = `
    <div style="${STYLES.container}">
      <div style="${STYLES.header} ${STYLES.headerDemographic}">
        <h3 style="${STYLES.title}">📊 ${
          escapeHtml(attributes.name as string) || "Population Area"
        }</h3>
      </div>
      <div style="${STYLES.content}">
        <div style="${STYLES.row}">
          <strong>Population:</strong> ${(
            (attributes.population as number) || 0
          ).toLocaleString()} people
        </div>
      </div>
    </div>
  `;

  return { title, content };
}

/**
 * Show provider land ownership without deriving fuel or hazard claims.
 */
export function createVegetationPopup(
  attributes: Record<string, unknown>,
): PopupResult {
  const title = escapeHtml(attributes.name as string) || "Federal land";
  const content = `
    <div style="${STYLES.container}">
      <div style="${STYLES.header} ${STYLES.headerVegetation}">
        <h3 style="${STYLES.title}">🌲 ${
          escapeHtml(attributes.name as string) || "Federal land"
        }</h3>
      </div>
      <div style="${STYLES.content}">
        <div style="${STYLES.row}">
          <strong>Agency:</strong> ${escapeHtml(attributes.agency as string) || "Unavailable"}
        </div>
        <p>${escapeHtml(attributes.source as string)}</p>
        <p>Land ownership does not establish vegetation, fuels or predicted fire behavior.</p>
      </div>
    </div>
  `;

  return { title, content };
}

/**
 * Create popup for center/location marker
 */
export function createCenterPopup(
  attributes: Record<string, unknown>,
): PopupResult {
  const title = `📍 ${escapeHtml(attributes.name as string) || "Location"}`;
  const content = `
    <div style="${STYLES.container}">
      <div style="${STYLES.header} ${STYLES.headerLocation}">
        <h3 style="${STYLES.title}">📍 Search Area</h3>
      </div>
      <div style="${STYLES.content}">
        <div style="${STYLES.row}">
          <strong>Location:</strong> ${escapeHtml(attributes.name as string) || "Center Point"}
        </div>
        ${formatWeatherSection(attributes.currentWeather as WeatherData | undefined)}
      </div>
    </div>
  `;

  return { title, content };
}
