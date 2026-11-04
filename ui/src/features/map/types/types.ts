/**
 * Map Component Types
 * Phase 1.1 of UI Refactoring Plan
 */

import type { components } from "@/schemas/generated/api-types";
import type {
  AssessmentResult, FireRiskJobReference, FireRiskPreparationError,
} from "@/features/fire-exposure/generated/contracts";
import type Map from "@arcgis/core/Map";
import type GraphicsLayer from "@arcgis/core/layers/GraphicsLayer";
import type MapView from "@arcgis/core/views/MapView";
import type { MapContextStatus } from "../services/mapContext";

// --- Location Types ---

export interface CenterLocation {
  latitude: number;
  longitude: number;
  name?: string;
}

// Map and assistant use the same nullable provider fields as the wire contract.
export type FireLocation = components["schemas"]["FireLocation"];
export type FireData = components["schemas"]["FireData"];
export type WeatherData = components["schemas"]["WeatherData"];

// --- Map Data Types ---

export interface MapData {
  locations: FireLocation[];
  fireExposureData?: AssessmentResult[];
  fireExposureJobs?: FireRiskJobReference[];
  fireExposureError?: FireRiskPreparationError;
}

// --- Layer Types ---

export interface MapLayers {
  riskZonesLayer: GraphicsLayer;
  locationsLayer: GraphicsLayer;
  firesLayer: GraphicsLayer;
  windLayer: GraphicsLayer;
  demographicLayer: GraphicsLayer;
  vegetationLayer: GraphicsLayer;
}

// --- Hook Return Types ---

export interface UseMapInitializationReturn {
  mapView: MapView | null;
  map: Map | null;
  layers: MapLayers | null;
  isLoading: boolean;
  error: string | null;
  contextStatus: MapContextStatus;
  terrainVisible: boolean;
  setTerrainVisible: (visible: boolean) => void;
  retryContext: () => void;
}
