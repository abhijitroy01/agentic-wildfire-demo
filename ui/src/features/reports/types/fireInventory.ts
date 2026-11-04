/**
 * Fire Inventory Data Models
 *
 * Fire metadata discovered from investigation answers and stage 2 scenarios: the "known fires" catalog.
 */

export interface GeoLocation {
  lat: number;
  lng: number;
}

/**
 * Known fire - discovered from agent queries
 */
export interface KnownFire {
  fireId: string;
  fireName: string;
  location: GeoLocation;
  acres?: number;
  containment?: number;
  lastSeen: string; // ISO timestamp
}
