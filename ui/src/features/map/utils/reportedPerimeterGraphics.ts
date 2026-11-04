import type Graphic from "@arcgis/core/Graphic";
import type Polygon from "@arcgis/core/geometry/Polygon";
import type SimpleFillSymbol from "@arcgis/core/symbols/SimpleFillSymbol";
import type { AssessmentResult } from "@/features/fire-exposure/generated/contracts";
import { arcgisExposureRings } from "./fireExposureGraphics";
import { escapeHtml } from "./popupGenerators";

interface PerimeterArcgis {
  Graphic: typeof Graphic;
  Polygon: typeof Polygon;
  SimpleFillSymbol: typeof SimpleFillSymbol;
}

/** Draw only supplied source geometry. A reported perimeter never replaces the point-initialized scenario. */
export function reportedPerimeterGraphics(arcgis: PerimeterArcgis, assessments: readonly AssessmentResult[])
  : Graphic[] {
  const seen = new Set<string>();
  return assessments.flatMap(({ fire }) => {
    if (!fire.perimeter) return [];
    const identity = JSON.stringify([fire.source_id, fire.latitude, fire.longitude,
      fire.source_timestamp, fire.perimeter]);
    if (seen.has(identity)) return [];
    seen.add(identity);
    return arcgisExposureRings(fire.perimeter).map(rings => new arcgis.Graphic({
      geometry: new arcgis.Polygon({ rings, spatialReference: { wkid: 4326 } }),
      symbol: new arcgis.SimpleFillSymbol({ style: "none",
        outline: { color: [23, 37, 84, 1], width: 2, style: "dash" } }),
      attributes: { type: "firePerimeter", fire_name: fire.name, source_id: fire.source_id,
        source_timestamp: fire.source_timestamp },
      popupTemplate: { title: "Reported perimeter", content: `<div>
        <strong>${escapeHtml(fire.name || "Reported fire")}</strong>
        <p>Source record updated: ${escapeHtml(fire.source_timestamp || "Time unavailable")}</p>
        <p>Perimeter capture time was not supplied. Modeled spread starts from the reported point.</p></div>` },
    }));
  });
}
