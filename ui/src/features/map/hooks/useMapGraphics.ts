import { logger } from "@/shared/utils/logger";
import type { ScenarioSelection } from "@/features/fire-exposure/services/assessment";
import type { AssessmentResult } from "@/features/fire-exposure/generated/contracts";
import type MapView from "@arcgis/core/views/MapView";
import { useCallback,useEffect,useRef,useState } from "react";
import type { CenterLocation,MapData,MapLayers } from "../types/types";
import {
  buildFireExposureGraphics, buildFireObservationGraphics, buildMapGraphics, showMapGraphics,
  buildMapOverlayGraphics, showMapOverlayGraphics, type MapGraphics, type OverlayState,
} from "../utils/mapGraphicsLoader";

interface UseMapGraphicsOptions {
  layers: MapLayers | null;
  mapData: MapData | null;
  centerLocation: CenterLocation | null;
  mapView: MapView | null;
  /** Fire and weather popups offer the camera action only while a camera is available. */
  cameraAvailable: boolean;
  exposureSelections?: ScenarioSelection;
  assessments?: readonly AssessmentResult[];
  displayedAssessments?: readonly AssessmentResult[];
}

/** Keep the map's graphics on the latest mapData: a load that a newer request replaced is dropped, never shown. */
export function useMapGraphics({
  layers,
  mapData,
  centerLocation,
  mapView,
  cameraAvailable,
  exposureSelections,
  assessments,
  displayedAssessments,
}: UseMapGraphicsOptions): {
  isLoading: boolean;
  error: string | null;
  overlayStatus: {demographic: OverlayState; vegetation: OverlayState} | null;
  retryOverlays: () => void;
  updateGraphics: () => Promise<void>;
} {
  const [isLoading, setIsLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [overlayStatus, setOverlayStatus] = useState<{demographic: OverlayState; vegetation: OverlayState} | null>(null);
  const [overlayRetry, setOverlayRetry] = useState(0);
  const shownOwner = useRef<MapData | null>(null);
  const latestRequest = useRef(0);
  const baseBuild = useRef<{
    mapData: MapData; center: CenterLocation | null; cameraAvailable: boolean; promise: Promise<MapGraphics>;
  } | null>(null);

  const updateGraphics = useCallback(async () => {
    if (!layers?.locationsLayer || !mapData) {
      return;
    }

    const request = ++latestRequest.current;
    setIsLoading(true);
    setError(null);
    const clearEvidence = (): void => {
      for (const layer of [layers.riskZonesLayer, layers.locationsLayer, layers.firesLayer, layers.windLayer]) {
        layer?.removeAll();
      }
    };
    if (shownOwner.current !== mapData) clearEvidence();

    try {
      // Selecting a returned scenario time reuses the same fire/weather/overlay graphics and in-flight build.
      if (!baseBuild.current || baseBuild.current.mapData !== mapData || baseBuild.current.center !== centerLocation
          || baseBuild.current.cameraAvailable !== cameraAvailable) {
        const promise = buildMapGraphics(mapData, centerLocation, {cameraAction: cameraAvailable}).catch(error => {
          if (baseBuild.current?.promise === promise) baseBuild.current = null;
          throw error;
        });
        baseBuild.current = { mapData, center: centerLocation, cameraAvailable, promise };
      }
      const base = await baseBuild.current.promise;
      const [riskZones, observations] = await Promise.all([
        buildFireExposureGraphics({ ...mapData, fireExposureData: displayedAssessments
          ? [...displayedAssessments] : mapData.fireExposureData }, exposureSelections),
        assessments ? buildFireObservationGraphics({ ...mapData, fireExposureData: [...assessments] },
          {cameraAction: cameraAvailable}) : Promise.resolve(null),
      ]);
      const graphics = { ...base, ...observations, riskZones };
      if (request !== latestRequest.current) return;
      await showMapGraphics(layers, graphics, {optional: false});
      shownOwner.current = mapData;
    } catch (error) {
      if (request !== latestRequest.current) return;
      logger.error("Graphics loading failed:", error);
      clearEvidence();
      setError(error instanceof Error ? error.message : "Map evidence could not be rendered.");
    } finally {
      if (request === latestRequest.current) setIsLoading(false);
    }
  }, [layers, mapData, centerLocation, cameraAvailable, exposureSelections, assessments, displayedAssessments]);

  useEffect(() => {
    if (!mapView || !layers?.locationsLayer || !mapData) {
      if (layers && !mapData) {
        for (const layer of [layers.riskZonesLayer, layers.locationsLayer, layers.firesLayer, layers.windLayer]) {
          layer?.removeAll();
        }
        shownOwner.current = null;
        baseBuild.current = null;
        setError(null);
        setIsLoading(false);
      }
      return;
    }

    void updateGraphics();
    return () => { latestRequest.current += 1; };
  }, [mapView, layers, mapData, updateGraphics]);

  useEffect(() => {
    if (!mapView || !layers || !centerLocation) {
      layers?.demographicLayer.removeAll(); layers?.vegetationLayer.removeAll();
      setOverlayStatus(null);
      return;
    }
    const controller = new AbortController();
    let current = true;
    layers.demographicLayer.removeAll(); layers.vegetationLayer.removeAll();
    setOverlayStatus({demographic: "loading", vegetation: "loading"});
    void buildMapOverlayGraphics(centerLocation, controller.signal).then(graphics => {
      if (!current) return;
      showMapOverlayGraphics(layers, graphics);
      setOverlayStatus(graphics.status);
    }).catch(error => {
      if (!current || controller.signal.aborted) return;
      logger.error("Optional map context could not be rendered:", error);
      setOverlayStatus({demographic: "unavailable", vegetation: "unavailable"});
    });
    return () => { current = false; controller.abort(); };
  }, [mapView, layers, centerLocation, overlayRetry]);

  return {
    isLoading,
    error,
    overlayStatus,
    retryOverlays: () => setOverlayRetry(value => value + 1),
    updateGraphics,
  };
}

export default useMapGraphics;
