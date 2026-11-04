import { DEFAULT_CENTER,DEFAULT_ZOOM } from "@/shared/constants";
import { logger } from "@/shared/utils/logger";
import type Map from "@arcgis/core/Map";
import type MapView from "@arcgis/core/views/MapView";
import { useCallback,useEffect,useRef,useState } from "react";
import type { MapLayers, UseMapInitializationReturn } from "../types/types";
import { createMapContext, type MapContext, type MapContextStatus } from "../services/mapContext";

interface UseMapInitializationOptions {
  containerRef: React.RefObject<HTMLDivElement>;
  onMapReady?: (map: Map, view: MapView) => void;
}

export function useMapInitialization({
  containerRef,
  onMapReady,
}: UseMapInitializationOptions): UseMapInitializationReturn {
  const [mapView, setMapView] = useState<MapView | null>(null);
  const [map, setMap] = useState<Map | null>(null);
  const [layers, setLayers] = useState<MapLayers | null>(null);
  const [isLoading, setIsLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [contextStatus, setContextStatus] = useState<MapContextStatus>({ streets: "loading", terrain: "loading" });
  const [terrainVisible, updateTerrainVisible] = useState(true);
  const terrainVisibleRef = useRef(true);
  const [contextRevision, setContextRevision] = useState(0);
  const contextRef = useRef<MapContext | null>(null);
  const retiredView = useRef<Promise<void>>(Promise.resolve());
  const setTerrainVisible = useCallback((visible: boolean): void => {
    terrainVisibleRef.current = visible;
    if (contextRef.current) contextRef.current.terrain.visible = visible;
    updateTerrainVisible(visible);
  }, []);
  // Recreate the same providers, including their load/view state; refresh cannot recover failed metadata.
  const retryContext = useCallback((): void => { setContextRevision(value => value + 1); }, []);

  useEffect(() => {
    let disposed = false;
    let createdView: MapView | null = null;

    const initMap = async (): Promise<void> => {
      try {
        setIsLoading(true);
        setError(null);
        setContextStatus({ streets: "loading", terrain: "loading" });

        if (!containerRef.current) {
          throw new Error("Map container not available");
        }
        // A retried view shares this DOM container; retire its previous owner before attaching another view.
        await retiredView.current;
        if (disposed) return;

        const [MapImport, ViewImport, GraphicsImport, ScaleImport, context] = await Promise.all([
          import("@arcgis/core/Map"), import("@arcgis/core/views/MapView"),
          import("@arcgis/core/layers/GraphicsLayer"), import("@arcgis/core/widgets/ScaleBar"),
          createMapContext((layer, status) => {
            if (!disposed) setContextStatus(previous => ({ ...previous, [layer]: status }));
          }),
        ]);
        if (disposed) { context.dispose(); context.basemap.destroy(); context.terrain.destroy(); return; }
        contextRef.current = context;
        context.terrain.visible = terrainVisibleRef.current;
        const MapModule = MapImport.default, MapViewModule = ViewImport.default;
        const GraphicsLayerModule = GraphicsImport.default;

        const riskZonesLayer = new GraphicsLayerModule({
          title: "Modeled spread scenario",
          opacity: 0.8,
          visible: true,
        });

        const locationsLayer = new GraphicsLayerModule({
          title: "Locations",
          visible: true,
        });

        const firesLayer = new GraphicsLayerModule({
          title: "Fires",
          visible: true,
        });

        const windLayer = new GraphicsLayerModule({
          title: "Wind",
          visible: true,
        });

        const demographicLayer = new GraphicsLayerModule({
          title: "Population Density",
          opacity: 0.5,
          visible: true,
        });

        const vegetationLayer = new GraphicsLayerModule({
          title: "Federal land boundaries",
          opacity: 0.7,
          visible: true,
        });

        const newMap = new MapModule({
          basemap: context.basemap,
          layers: [
            context.terrain,
            demographicLayer,
            vegetationLayer,
            riskZonesLayer,
            locationsLayer,
            firesLayer,
            windLayer,
          ],
        });

        const view = new MapViewModule({
          container: containerRef.current,
          map: newMap as unknown as __esri.MapProperties,
          center: [DEFAULT_CENTER.longitude, DEFAULT_CENTER.latitude],
          zoom: DEFAULT_ZOOM,
          constraints: { maxZoom: 18 },
        });

        createdView = view;
        await view.when();
        if (disposed) return;
        view.ui.add(new ScaleImport.default({ view, unit: "dual", style: "line" }), "bottom-left");

        if (view.container) {
          view.container.style.outline = "none";
          view.container.style.border = "none";
          view.container.style.boxShadow = "none";
          view.container.setAttribute("tabindex", "-1");
          const container = view.container;
          container.onfocus = () => container.blur();
        }

        // Configure popup settings
        if (view.popup) {
          (
            view.popup as __esri.Popup & {
              autoOpenEnabled: boolean;
              paginationVisible: boolean;
            }
          ).autoOpenEnabled = true;
          view.popup.dockEnabled = true;
          view.popup.dockOptions = {
            buttonEnabled: true,
            breakpoint: { width: 544, height: 544 },
          };
          (
            view.popup as __esri.Popup & {
              autoOpenEnabled: boolean;
              paginationVisible: boolean;
            }
          ).paginationVisible = false;
        }

        const layersObj: MapLayers = {
          riskZonesLayer,
          locationsLayer,
          firesLayer,
          windLayer,
          demographicLayer,
          vegetationLayer,
        };

        setMap(newMap);
        setMapView(view);
        setLayers(layersObj);
        setIsLoading(false);

        onMapReady?.(newMap, view);
      } catch (err) {
        if (disposed) return;
        const errorMessage =
          err instanceof Error ? err.message : "Map initialization failed";
        logger.error("Map initialization error:", err);
        setError(errorMessage);
        setIsLoading(false);
      }
    };

    initMap();

    return () => {
      disposed = true;
      contextRef.current?.dispose();
      contextRef.current = null;
      // A detached/hidden container may never make view.when() resolve. Layer loading does not need the DOM;
      // drain it first, then any layer views that started while this view was ready.
      if (createdView) {
        const view = createdView;
        retiredView.current = (async () => {
          try {
            const retiringLayers = view.map?.allLayers.toArray() || [];
            await Promise.allSettled(retiringLayers.map(layer => layer.load()));
            if (view.ready) await Promise.allSettled(retiringLayers.map(layer => view.whenLayerView(layer)));
          } finally { view.destroy(); }
        })().catch(() => undefined);
      }
    };
  }, [containerRef, onMapReady, contextRevision]);

  return {
    mapView,
    map,
    layers,
    isLoading,
    error,
    contextStatus,
    terrainVisible,
    setTerrainVisible,
    retryContext,
  };
}

export default useMapInitialization;
