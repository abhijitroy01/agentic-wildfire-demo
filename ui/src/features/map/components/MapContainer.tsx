import type Graphic from "@arcgis/core/Graphic";
import type Map from "@arcgis/core/Map";
import type MapView from "@arcgis/core/views/MapView";
import type React from "react";
import { useCallback,useEffect,useMemo,useRef,useState } from "react";

import { useCameraAvailable } from "@/features/camera/hooks/useCameraAvailable";
import { weatherQueriedAt } from "@/shared/domain/incidentObservations";
import FireExposureSummary, { type FireExposureChoice } from "@/features/fire-exposure/components/FireExposureSummary";
import { useFireRiskJobs } from "@/features/fire-exposure/hooks/useFireRiskJobs";
import {
  answerAssessments, visibleExposureFeatures, scenarioHorizons, type ScenarioSelection,
} from "@/features/fire-exposure/services/assessment";
import { useMapCentering } from "../hooks/useMapCentering";
import { useMapGraphics } from "../hooks/useMapGraphics";
import { useMapInitialization } from "../hooks/useMapInitialization";
import { useMapPopups } from "../hooks/useMapPopups";
import type { CenterLocation,MapData } from "../types/types";
import "./MapContainer.css";
import MapLegend from "./MapLegend";
import MapContextControls from "./MapContextControls";

interface MapContainerProps {
  mapData: MapData | null;
  centerLocation: CenterLocation | null;
  className?: string;
  onMapReady?: (map: Map, view: MapView) => void;
  onFeatureClick?: (graphic: Graphic) => void;
  activeTrucks?: unknown[];
  onSimulateReportedFire?: (assessmentId: string) => void;
}

export function MapContainer({
  mapData,
  centerLocation,
  className,
  onMapReady,
  onFeatureClick,
  activeTrucks = [],
  onSimulateReportedFire,
}: MapContainerProps): React.ReactElement {
  const containerRef = useRef<HTMLDivElement>(null);
  const [selection, setSelection] = useState<{
    owner: MapData | null; fire: string | null; scenarios: ScenarioSelection;
  }>({owner: null, fire: null, scenarios: {}});
  const exposureSelections = useMemo<ScenarioSelection>(() =>
    selection.owner === mapData ? selection.scenarios : {}, [selection, mapData]);
  const { assessments, jobs, refreshStatus, cancelJob, retryJob } = useFireRiskJobs(mapData);
  const choices = useMemo<FireExposureChoice[]>(() => (mapData?.locations ?? []).flatMap((location, index) =>
    location.fire && Number.isFinite(location.latitude) && Number.isFinite(location.longitude)
      && Math.abs(location.latitude) <= 90 && Math.abs(location.longitude) <= 180
      ? [{ key: String(index), label: location.fire.name || `Reported fire ${index + 1}`,
        latitude: location.latitude, longitude: location.longitude }] : []), [mapData]);
  const selectedFire = selection.owner === mapData && choices.some(choice => choice.key === selection.fire)
    ? selection.fire : choices[0]?.key;
  const selectedLocation = selectedFire == null ? undefined : mapData?.locations[Number(selectedFire)];
  const displayedAssessments = useMemo(() =>
    answerAssessments(assessments, selectedLocation ? [selectedLocation] : []), [assessments, selectedLocation]);
  // Fit the selected fire and its complete saved terrain footprint once on selection/hydration, never on time changes.
  const focusData = useMemo<MapData | null>(() => selectedLocation ? { locations: [selectedLocation],
    fireExposureData: displayedAssessments.filter(item => ["landscape", "surface_fire"].includes(item.mode)) }
    : null, [selectedLocation, displayedAssessments]);
  const selectScenarioTime = useCallback((assessmentId: string, minute: number | null): void => {
    setSelection(previous => ({ owner: mapData, fire: previous.owner === mapData ? previous.fire : null,
      scenarios: { ...(previous.owner === mapData ? previous.scenarios : {}), [assessmentId]: minute } }));
  }, [mapData]);
  const selectFire = useCallback((fire: string): void => {
    setSelection(previous => ({owner: mapData, fire,
      scenarios: previous.owner === mapData ? previous.scenarios : {}}));
  }, [mapData]);

  const { mapView, layers, isLoading, error, contextStatus, terrainVisible, setTerrainVisible, retryContext }
    = useMapInitialization({
    containerRef,
    onMapReady,
  });

  // Keyed on values: a new center object per parent render (each telemetry frame) must not reload or re-fit the map.
  const latitude = centerLocation?.latitude, longitude = centerLocation?.longitude, name = centerLocation?.name;
  const center = useMemo<CenterLocation | null>(
    () => latitude === undefined || longitude === undefined ? null : { latitude, longitude, name },
    [latitude, longitude, name],
  );

  // Fire and weather popups offer "View Camera Feed" only while the workspace reports a configured camera.
  const cameraAvailable = useCameraAvailable();

  const { isLoading: graphicsLoading, error: graphicsError, updateGraphics, overlayStatus, retryOverlays } = useMapGraphics({
    layers,
    mapData,
    centerLocation: center,
    mapView,
    cameraAvailable,
    exposureSelections,
    assessments,
    displayedAssessments,
  });

  useMapCentering({
    mapView,
    centerLocation: focusData ? null : center,
    mapData: focusData ?? mapData,
    terrainFocus: displayedAssessments.some(item => item.mode === "landscape" && item.availability === "available"),
  });

  const { handleMapClick } = useMapPopups({
    mapView,
    onFeatureClick,
    cameraAvailable,
  });

  useEffect(() => {
    if (!mapView) return;

    const clickHandle = mapView.on("click", handleMapClick);

    return () => {
      clickHandle.remove();
    };
  }, [mapView, handleMapClick]);

  const hasWindData = mapData?.locations.some(location => {
    const retained = assessments.filter(item =>
      item.fire.latitude === location.latitude && item.fire.longitude === location.longitude);
    return retained?.length ? retained.some(item => item.weather?.availability === "available"
      && Math.abs(item.weather.query_latitude - location.latitude) < 1e-7
      && Math.abs(item.weather.query_longitude - location.longitude) < 1e-7
      && typeof item.weather.wind_speed_mps === "number" && item.weather.wind_speed_mps > 0
      && typeof item.weather.wind_from_degrees === "number")
      : Boolean(weatherQueriedAt(location.currentWeather, location.latitude, location.longitude)
        && typeof location.currentWeather?.wind?.speed?.value === "number"
        && location.currentWeather.wind.speed.value > 0
        && typeof location.currentWeather.wind.direction?.value === "number");
  });
  const visibleFeatures = displayedAssessments.flatMap(item => visibleExposureFeatures(item, exposureSelections));
  const landscapeMinute = visibleFeatures.find(item => item.properties.kind === "landscape")?.properties.elapsed_minutes;
  const landscapeHorizons = [...new Set(displayedAssessments.filter(item =>
    item.mode === "landscape" && item.availability === "available").flatMap(scenarioHorizons))].sort((a, b) => a - b);
  const surfaceFireMinute = visibleFeatures.find(item => item.properties.kind === "surface_fire")
    ?.properties.elapsed_minutes;
  const surfaceFireHorizons = [...new Set(displayedAssessments.filter(item =>
    item.mode === "surface_fire" && item.availability === "available").flatMap(scenarioHorizons))].sort((a, b) => a - b);

  return (
    <div className={`map-container-wrapper ${className || ""}`}>
      <div ref={containerRef} className="map-view-container" />

      {(isLoading || graphicsLoading) && (
        <div className="map-loading-overlay">
          <div className="map-loading-spinner" />
          <div className="map-loading-text">
            {isLoading ? "Initializing map..." : "Loading map data..."}
          </div>
        </div>
      )}

      {error && (
        <div className="map-error-overlay">
          <div className="map-error-icon">⚠️</div>
          <div className="map-error-message">{error}</div>
          <button
            className="map-error-retry"
            onClick={() => window.location.reload()}
          >
            Retry
          </button>
        </div>
      )}

      {graphicsError && !error && <div className="map-error-overlay" role="alert">
        <p>Map evidence unavailable: {graphicsError}</p>
        <button type="button" onClick={() => void updateGraphics()}>Retry map evidence</button>
      </div>}
      {mapView && !isLoading && !error && (
        <MapLegend 
          showWind={hasWindData} 
          showFireExposure={visibleFeatures.some(item => ["proximity", "downwind"].includes(item.properties.kind))}
          showSurfaceFire={visibleFeatures.some(item => item.properties.kind === "surface_fire")}
          showLandscape={visibleFeatures.some(item => item.properties.kind === "landscape")}
          landscapeMinute={landscapeMinute ?? undefined}
          landscapeHorizons={landscapeHorizons}
          surfaceFireMinute={surfaceFireMinute ?? undefined}
          surfaceFireHorizons={surfaceFireHorizons}
          showObservedPerimeter={answerAssessments(assessments, mapData?.locations ?? [])
            .some(item => item.fire.perimeter != null)}
          showResponseUnits={Array.isArray(activeTrucks) && activeTrucks.length > 0}
        />
      )}
      {mapView && !isLoading && !error && contextStatus && <MapContextControls status={contextStatus}
        terrainVisible={terrainVisible} onTerrainVisible={setTerrainVisible} onRetry={retryContext}
        overlayStatus={overlayStatus} onRetryOverlays={retryOverlays} />}
      {mapView && !isLoading && !error && <FireExposureSummary assessments={displayedAssessments}
        selections={exposureSelections} onScenarioTime={selectScenarioTime}
        choices={choices} selectedFire={selectedFire ?? undefined} onSelectFire={selectFire}
        jobs={jobs} onRefreshStatus={refreshStatus}
        preparationError={mapData?.fireExposureError}
        onCancelJob={cancelJob}
        onRetryJob={retryJob}
        onSimulateReportedFire={onSimulateReportedFire} />}
    </div>
  );
}

export default MapContainer;
