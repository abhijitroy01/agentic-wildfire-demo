import TruckMapOverlay from "@/features/firetruck/components/TruckMapOverlay";
import LazyMapContainer from "@/features/map/components/LazyMapContainer";
import type { TruckView } from "@/features/firetruck/utils/truckDisplayUtils";
import type { CenterLocation, MapData } from "@/features/map/types/types";
import { useMapFullscreen } from "@/shared/hooks/useMapFullscreen";
import type MapView from "@arcgis/core/views/MapView";
import { useCallback, useState } from "react";

/** Shared map composition for investigation and response exercises, including undocking. */
export default function WorkspaceMap({ mapData, centerLocation, trucks = [], label = "investigation",
  onSimulateReportedFire }: {
  mapData: MapData | null; centerLocation: CenterLocation | null; trucks?: TruckView[]; label?: string;
  onSimulateReportedFire?: (assessmentId: string) => void;
}): React.ReactElement {
  const [view, setView] = useState<MapView | null>(null);
  const fullscreen = useMapFullscreen();
  const ready = useCallback((_map: unknown, next: MapView) => setView(next), []);
  return <div ref={fullscreen.containerRef} role={fullscreen.expanded ? "dialog" : undefined}
    aria-modal={fullscreen.expanded || undefined} aria-label={`${label} map`}
    className={`map-container ${fullscreen.expanded ? "map-undocked" : ""}`}>
    <button data-map-fullscreen className="map-undock-control" onClick={fullscreen.toggle} aria-expanded={fullscreen.expanded}>
      {fullscreen.expanded ? `Restore ${label} map` : `Enlarge ${label} map`}
    </button>
    <LazyMapContainer mapData={mapData} centerLocation={centerLocation}
      className="main-map" onMapReady={ready} activeTrucks={trucks}
      onSimulateReportedFire={onSimulateReportedFire} />
    <TruckMapOverlay trucks={trucks} mapView={view} />
  </div>;
}
