import { useId,useState } from "react";
import type { IncidentCoordinates } from "../generated/contracts";

/** Explicit coordinates carry no geocoder confidence; the server verifies U.S. simulation coverage. */
export function scenarioCoordinates(latitude: string, longitude: string): IncidentCoordinates | null {
  if (!latitude.trim() || !longitude.trim()) return null;
  const lat = Number(latitude), lon = Number(longitude);
  return Number.isFinite(lat) && Number.isFinite(lon) && Math.abs(lat) <= 90 && Math.abs(lon) <= 180
    ? {lat,lon} : null;
}

export default function CoordinateEntry({ busy, onResolve }: {
  busy: boolean; onResolve: (point: IncidentCoordinates) => void;
}): React.ReactElement {
  const id = useId();
  const [latitude, setLatitude] = useState(""), [longitude, setLongitude] = useState("");
  const point = scenarioCoordinates(latitude, longitude);
  return <details><summary>Enter a scenario coordinate</summary>
    <p>Use a U.S. latitude and longitude. This assumes a scenario fire at the entered point.</p>
    <label htmlFor={`${id}-lat`}>Latitude<input id={`${id}-lat`} type="number" min="-90" max="90"
      step="any" value={latitude} onChange={event => setLatitude(event.target.value)} /></label>
    <label htmlFor={`${id}-lon`}>Longitude<input id={`${id}-lon`} type="number" min="-180" max="180"
      step="any" value={longitude} onChange={event => setLongitude(event.target.value)} /></label>
    <button type="button" disabled={busy || !point} onClick={() => {
      if (point) onResolve(point);
    }}>Use coordinate &amp; prepare</button>
  </details>;
}
