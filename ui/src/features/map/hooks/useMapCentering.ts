import { TIMING } from "@/shared/constants";
import { logger } from "@/shared/utils/logger";
import type MapView from "@arcgis/core/views/MapView";
import { useCallback,useEffect,useRef } from "react";
import type { CenterLocation,MapData } from "../types/types";
import { fireExposureInitialExtent } from "../utils/fireExposureExtent";

interface UseMapCenteringOptions {
  mapView: MapView | null;
  centerLocation: CenterLocation | null;
  mapData: MapData | null;
  terrainFocus?: boolean;
}

export function useMapCentering({
  mapView,
  centerLocation,
  mapData,
  terrainFocus = false,
}: UseMapCenteringOptions): {
  centerMap: () => Promise<void>;
  isActive: boolean;
} {
  const debounceTimer = useRef<NodeJS.Timeout | null>(null);
  const centeringInProgress = useRef<boolean>(false);
  // A target that changes during a fit is applied when that fit ends, never dropped.
  const recenterWhenDone = useRef(false);
  const latestCenterMap = useRef<() => Promise<void>>(async () => undefined);

  const centerMap = useCallback(async () => {
    if (!mapView) return;

    centeringInProgress.current = true;

    try {
      const exposure = mapData && fireExposureInitialExtent(mapData, centerLocation,
        {width: mapView.width, height: mapView.height, terrainFocus});
      if (exposure) {
        const Extent = (await import("@arcgis/core/geometry/Extent")).default;
        const {zoom, ...bounds} = exposure;
        await mapView.goTo({target: new Extent({...bounds, spatialReference: {wkid: 4326}}), zoom},
          {duration: TIMING.MAP_CENTER_ANIMATION_MS, pickClosestTarget: true});
        return;
      }
      if (mapData?.locations && mapData.locations.length > 0) {
        const [Point, geometryEngineModule, Extent] = await Promise.all([
          import("@arcgis/core/geometry/Point").then((m) => m.default),
          import("@arcgis/core/geometry/geometryEngine"),
          import("@arcgis/core/geometry/Extent").then((m) => m.default),
        ]);
        const geometryEngine = geometryEngineModule;

        const points = mapData.locations.map(
          (loc) =>
            new Point({
              longitude: loc.longitude,
              latitude: loc.latitude,
              spatialReference: { wkid: 4326 },
            }),
        );

        if (centerLocation) {
          points.push(
            new Point({
              longitude: centerLocation.longitude,
              latitude: centerLocation.latitude,
              spatialReference: { wkid: 4326 },
            }),
          );
        }

        if (points.length === 0) return;

        if (points.length === 1) {
          await mapView.goTo(
            { target: points[0], zoom: 12 },
            { duration: TIMING.MAP_CENTER_ANIMATION_MS },
          );
        } else {
          const unionResult = geometryEngine.union(points);
          let targetExtent;

          if (unionResult?.extent) {
            targetExtent = unionResult.extent;
          } else {
            const lons = points
              .map((p) => p.longitude)
              .filter((v): v is number => v != null);
            const lats = points
              .map((p) => p.latitude)
              .filter((v): v is number => v != null);
            targetExtent = new Extent({
              xmin: Math.min(...lons) - 0.5,
              xmax: Math.max(...lons) + 0.5,
              ymin: Math.min(...lats) - 0.5,
              ymax: Math.max(...lats) + 0.5,
              spatialReference: { wkid: 4326 },
            });
          }

          await mapView.goTo(
            { target: targetExtent.expand(1.5) },
            { duration: TIMING.MAP_CENTER_ANIMATION_MS },
          );
        }
      } else if (centerLocation) {
        const Point = (await import("@arcgis/core/geometry/Point")).default;
        const point = new Point({
          longitude: centerLocation.longitude,
          latitude: centerLocation.latitude,
          spatialReference: { wkid: 4326 },
        });

        await mapView.goTo(
          { target: point, zoom: 12 },
          { duration: TIMING.MAP_CENTER_ANIMATION_MS },
        );
      }
    } catch (error) {
      // The map unmounted mid-fit (for example on a quick change of workflow stage): nothing is left to center.
      if (mapView.destroyed) return;
      logger.error("Map centering failed:", error);

      if (centerLocation) {
        try {
          const Point = (await import("@arcgis/core/geometry/Point")).default;
          const point = new Point({
            longitude: centerLocation.longitude,
            latitude: centerLocation.latitude,
            spatialReference: { wkid: 4326 },
          });

          await mapView.goTo(
            { target: point, zoom: 12 },
            { duration: TIMING.CENTER_FALLBACK_DURATION_MS },
          );
        } catch (retryError) {
          logger.error("Map centering fallback failed:", retryError);
        }
      }
    } finally {
      centeringInProgress.current = false;
      if (recenterWhenDone.current) {
        recenterWhenDone.current = false;
        void latestCenterMap.current();
      }
    }
  }, [mapView, centerLocation, mapData, terrainFocus]);
  latestCenterMap.current = centerMap;

  useEffect(() => {
    if (!mapView) return;

    if (
      !centerLocation &&
      (!mapData?.locations || mapData.locations.length === 0)
    ) {
      return;
    }

    if (centeringInProgress.current) {
      recenterWhenDone.current = true;
      return () => {
        recenterWhenDone.current = false;
      };
    }

    if (debounceTimer.current) {
      clearTimeout(debounceTimer.current);
    }

    debounceTimer.current = setTimeout(() => {
      centerMap();
    }, TIMING.MAP_DEBOUNCE_MS);

    return () => {
      if (debounceTimer.current) {
        clearTimeout(debounceTimer.current);
      }
    };
  }, [mapView, centerLocation, mapData, centerMap]);

  return {
    centerMap,
    isActive: centeringInProgress.current,
  };
}

export default useMapCentering;
