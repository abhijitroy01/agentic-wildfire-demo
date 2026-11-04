import type Basemap from "@arcgis/core/Basemap";
import type BaseDynamicLayer from "@arcgis/core/layers/BaseDynamicLayer";
import { TERRAIN_RENDERING_FUNCTION, TERRAIN_SERVICE_URL } from "@/shared/config";

export type ContextStatus = "loading" | "ready" | "failed";
export interface MapContextStatus {
  streets: ContextStatus;
  terrain: ContextStatus;
}

/** ArcGIS uses this standard name when navigation cancels an outstanding request. */
export function isMapRequestAbort(error: unknown): boolean {
  return typeof error === "object" && error !== null && "name" in error && error.name === "AbortError";
}

export function hasVisibleTerrainPixels(pixels: ArrayLike<number>): boolean {
  for (let alpha = 3; alpha < pixels.length; alpha += 4) if (pixels[alpha] > 0) return true;
  return false;
}

/** Transparent no-data imagery is unavailable; a valid uniformly shaded flat surface remains valid. */
function requireTerrainImage(image: HTMLImageElement | HTMLCanvasElement): HTMLImageElement | HTMLCanvasElement {
  if (!image.width || !image.height) throw new Error("USGS terrain imagery has no pixels for this map extent.");
  const canvas = document.createElement("canvas");
  canvas.width = image.width;
  canvas.height = image.height;
  const context = canvas.getContext("2d", { willReadFrequently: true });
  if (!context) throw new Error("Terrain imagery could not be inspected.");
  context.drawImage(image, 0, 0);
  if (!hasVisibleTerrainPixels(context.getImageData(0, 0, canvas.width, canvas.height).data)) {
    throw new Error("USGS terrain imagery has no coverage for this map extent.");
  }
  return image;
}

/** A failed image remains explicit until that request succeeds or the user retries the same provider. */
export class ContextRequests {
  private pending = 0;
  private received = false;
  private failures = new Set<string>();
  private generation = 0;

  constructor(private publish: (status: ContextStatus) => void) {}

  private changed(): void {
    this.publish(this.failures.size ? "failed" : this.pending || !this.received ? "loading" : "ready");
  }

  fail(key: string): void {
    this.failures.add(key);
    this.changed();
  }

  reset(): void {
    this.generation += 1;
    this.pending = 0;
    this.received = false;
    this.failures.clear();
    this.changed();
  }

  async run<T>(key: string, request: () => Promise<T>, isAbort: (error: unknown) => boolean): Promise<T> {
    const generation = this.generation;
    this.pending += 1;
    this.changed();
    try {
      const result = await request();
      if (generation === this.generation) {
        this.received = true;
        this.failures.delete(key);
      }
      return result;
    } catch (error) {
      if (generation === this.generation && !isAbort(error)) this.failures.add(key);
      throw error;
    } finally {
      if (generation === this.generation) {
        this.pending -= 1;
        this.changed();
      }
    }
  }
}

export interface MapContext {
  basemap: Basemap;
  terrain: BaseDynamicLayer;
  dispose: () => void;
}

/** Observe native layer requests; never substitute another provider or change simulation evidence. */
export async function createMapContext(publish: (layer: keyof MapContextStatus, status: ContextStatus) => void)
  : Promise<MapContext> {
  const [BasemapModule, StreetModule, TerrainModule, requestModule] = await Promise.all([
    import("@arcgis/core/Basemap"), import("@arcgis/core/layers/OpenStreetMapLayer"),
    import("@arcgis/core/layers/BaseDynamicLayer"), import("@arcgis/core/request"),
  ]);
  let disposed = false;
  const controller = new AbortController();
  const notify = (layer: keyof MapContextStatus, status: ContextStatus): void => {
    if (!disposed) publish(layer, status);
  };
  const streets = new StreetModule.default({ id: "street-context", title: "OpenStreetMap" });
  const terrain = new TerrainModule.default({
    id: "terrain-context",
    title: "USGS terrain shading",
    // Verified public service extent. Pixels outside its actual coverage remain transparent and unavailable.
    fullExtent: { xmin: -20037507.0672, ymin: -1689391.823360186,
      xmax: 20037507.532527123, ymax: 18809001.9719, spatialReference: { wkid: 3857 } },
    opacity: 0.35,
    blendMode: "multiply",
  });
  const streetRequests = new ContextRequests(status => notify("streets", status));
  const terrainRequests = new ContextRequests(status => notify("terrain", status));
  const fetchTile = streets.fetchTile.bind(streets);
  streets.fetchTile = (...args) => streetRequests.run(args.slice(0, 3).join("/"),
    () => fetchTile(...args), isMapRequestAbort);
  const imageKey = (extent: __esri.Extent, width: number, height: number): string =>
    [extent.xmin, extent.ymin, extent.xmax, extent.ymax, width, height].join("/");
  terrain.fetchImage = (extent, width, height, options) => terrainRequests.run(imageKey(extent, width, height),
    async () => {
      // Direct supported image operation; no SDK raster metadata probes or alternate imagery source.
      const response = await requestModule.default(`${TERRAIN_SERVICE_URL}/exportImage`, {
        query: {
          bbox: [extent.xmin, extent.ymin, extent.xmax, extent.ymax].join(","),
          bboxSR: extent.spatialReference.wkid, imageSR: extent.spatialReference.wkid,
          size: `${width},${height}`, format: "png32", f: "image",
          renderingRule: JSON.stringify({ rasterFunction: TERRAIN_RENDERING_FUNCTION }),
        },
        responseType: "image",
        authMode: "anonymous",
        withCredentials: false,
        signal: options?.signal ? AbortSignal.any([options.signal, controller.signal]) : controller.signal,
      });
      return requireTerrainImage(response.data);
    }, isMapRequestAbort);
  const handles = [
    streets.on("layerview-create-error", () => streetRequests.fail("layer")),
    terrain.on("layerview-create-error", () => terrainRequests.fail("layer")),
  ];
  return {
    basemap: new BasemapModule.default({ baseLayers: [streets], title: "OpenStreetMap", id: "osm" }),
    terrain,
    dispose: () => {
      disposed = true;
      controller.abort();
      streetRequests.reset();
      terrainRequests.reset();
      handles.forEach(handle => handle.remove());
    },
  };
}
