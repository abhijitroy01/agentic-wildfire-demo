import type React from "react";
import type { MapContextStatus } from "../services/mapContext";
import type { OverlayState } from "../utils/mapGraphicsLoader";

interface MapContextControlsProps {
  status: MapContextStatus;
  terrainVisible: boolean;
  onTerrainVisible: (visible: boolean) => void;
  onRetry: () => void;
  overlayStatus?: {demographic: OverlayState; vegetation: OverlayState} | null;
  onRetryOverlays?: () => void;
}

export default function MapContextControls({ status, terrainVisible, onTerrainVisible, onRetry, overlayStatus, onRetryOverlays }
  : MapContextControlsProps): React.ReactElement {
  const failed = status.streets === "failed" || terrainVisible && status.terrain === "failed";
  return <aside className="map-context-controls" aria-label="Map context"
    data-streets-status={status.streets} data-terrain-status={status.terrain}>
    <div role="status" aria-live="polite">
      {status.streets === "loading" && <p>Street map loading…</p>}
      {status.streets === "failed" && <p>Street map tiles unavailable.</p>}
      {terrainVisible && status.terrain === "loading" && <p>Terrain shading loading…</p>}
      {terrainVisible && status.terrain === "failed" && <p>Terrain shading unavailable.</p>}
    </div>
    {failed && <button type="button" onClick={onRetry}>Retry map context</button>}
    <span>OpenStreetMap · USGS terrain</span>
    {overlayStatus && <div aria-label="Optional map overlays">
      {overlayStatus.demographic === "loading" && <p role="status">Population context loading…</p>}
      {overlayStatus.vegetation === "loading" && <p role="status">Federal land context loading…</p>}
      {overlayStatus.demographic === "unavailable" && <p>Population context unavailable.</p>}
      {overlayStatus.vegetation === "unavailable" && <p>Federal land context unavailable.</p>}
      {(overlayStatus.demographic === "unavailable" || overlayStatus.vegetation === "unavailable")
        && <button type="button" onClick={onRetryOverlays}>Retry optional overlays</button>}
    </div>}
    <label><input type="checkbox" checked={terrainVisible}
      onChange={event => onTerrainVisible(event.target.checked)} /> Terrain shading</label>
  </aside>;
}
