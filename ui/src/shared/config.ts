/**
 * Configuration for the application
 *
 * Investigation requests always use the same-origin server broker.
 * Configuration is driven by environment variables (.env file).
 *
 * Vite exposes env variables via import.meta.env with VITE_ prefix.
 * @see https://vitejs.dev/guide/env-and-mode.html
 */

// Data Service URLs (ArcGIS Living Atlas)
export const POPULATION_SERVICE_URL: string =
  import.meta.env.VITE_POPULATION_SERVICE_URL ||
  "https://services.arcgis.com/P3ePLMYs2RVChkJx/arcgis/rest/services/USA_Counties_Generalized_Boundaries/FeatureServer/0";

export const VEGETATION_SERVICE_URL: string =
  import.meta.env.VITE_VEGETATION_SERVICE_URL ||
  "https://services.arcgis.com/P3ePLMYs2RVChkJx/arcgis/rest/services/USA_Forest_Service_Lands/FeatureServer/0";

/** Public terrain context only; the model retains its own sealed LANDFIRE elevation input. */
export const TERRAIN_SERVICE_URL =
  "https://elevation.nationalmap.gov/arcgis/rest/services/3DEPElevation/ImageServer";
export const TERRAIN_RENDERING_FUNCTION = "Hillshade Multidirectional";

/** This build includes Evacuation & Crew Access with Guided and Research workflows. */
export const SIMULATION_ENABLED: boolean = import.meta.env.VITE_EVACUATION_ENABLED === "true";
