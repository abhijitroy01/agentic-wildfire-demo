/**
 * LazyMapContainer - Code Splitting for Map Component
 * Phase 3.1 - Performance Optimization
 *
 * This component provides lazy loading of the map functionality
 * to improve initial bundle size and load times.
 */

import { logger } from "@/shared/utils/logger";
import type Graphic from "@arcgis/core/Graphic";
import type Map from "@arcgis/core/Map";
import type MapView from "@arcgis/core/views/MapView";
import React,{ Suspense,lazy,memo } from "react";
import type { CenterLocation,MapData } from "../types/types";

// Lazy load the MapContainer
const MapComponent = lazy(() => import("./MapContainer"));

interface LazyMapContainerProps {
  mapData: MapData | null;
  centerLocation: CenterLocation | null;
  className?: string;
  onMapReady?: (map: Map, view: MapView) => void;
  onFeatureClick?: (graphic: Graphic) => void;
  activeTrucks?: unknown[];
  onSimulateReportedFire?: (assessmentId: string) => void;
}

/**
 * Loading fallback component for the map
 */
const MapLoadingFallback: React.FC = () => (
  <div className="map-loading-fallback">
    <div className="map-loading-fallback__container">
      <div className="map-loading-fallback__spinner"></div>
      <div className="map-loading-fallback__text">Loading Map...</div>
    </div>
    <style>{`
      .map-loading-fallback {
        width: 100%;
        height: 100%;
        min-height: 400px;
        display: flex;
        align-items: center;
        justify-content: center;
        background: linear-gradient(135deg, #f5f7fa 0%, #e4e8ec 100%);
        border-radius: 8px;
      }

      .map-loading-fallback__container {
        text-align: center;
      }

      .map-loading-fallback__spinner {
        width: 48px;
        height: 48px;
        border: 4px solid #e2e8f0;
        border-top-color: #3b82f6;
        border-radius: 50%;
        animation: spin 1s linear infinite;
        margin: 0 auto 16px;
      }

      .map-loading-fallback__text {
        color: #64748b;
        font-size: 14px;
        font-weight: 500;
      }

      @keyframes spin {
        to {
          transform: rotate(360deg);
        }
      }
    `}</style>
  </div>
);

/**
 * Error boundary fallback for map loading errors
 */
const MapErrorFallback: React.FC<{ error?: Error; retry?: () => void }> = ({
  error,
  retry,
}) => (
  <div className="map-error-fallback">
    <div className="map-error-fallback__icon">⚠️</div>
    <h3 className="map-error-fallback__title">Map Failed to Load</h3>
    <p className="map-error-fallback__message">
      {error?.message || "An error occurred while loading the map component."}
    </p>
    {retry && (
      <button className="map-error-fallback__button" onClick={retry}>
        Reload map
      </button>
    )}
    <style>{`
      .map-error-fallback {
        width: 100%;
        height: 100%;
        min-height: 400px;
        display: flex;
        flex-direction: column;
        align-items: center;
        justify-content: center;
        background: #fef2f2;
        border: 1px solid #fecaca;
        border-radius: 8px;
        padding: 24px;
        text-align: center;
      }

      .map-error-fallback__icon {
        font-size: 48px;
        margin-bottom: 16px;
      }

      .map-error-fallback__title {
        color: #dc2626;
        font-size: 18px;
        font-weight: 600;
        margin: 0 0 8px 0;
      }

      .map-error-fallback__message {
        color: #7f1d1d;
        font-size: 14px;
        margin: 0 0 16px 0;
        max-width: 400px;
      }

      .map-error-fallback__button {
        background: #dc2626;
        color: white;
        border: none;
        padding: 8px 24px;
        border-radius: 6px;
        font-size: 14px;
        font-weight: 500;
        cursor: pointer;
        transition: background 0.2s;
      }

      .map-error-fallback__button:hover {
        background: #b91c1c;
      }
    `}</style>
  </div>
);

/**
 * Error Boundary for catching map component errors
 */
class MapErrorBoundary extends React.Component<
  { children: React.ReactNode; fallback?: React.ReactNode },
  { hasError: boolean; error: Error | null }
> {
  constructor(props: { children: React.ReactNode; fallback?: React.ReactNode }) {
    super(props);
    this.state = { hasError: false, error: null };
  }

  static getDerivedStateFromError(error: Error): { hasError: boolean; error: Error } {
    return { hasError: true, error };
  }

  componentDidCatch(error: Error, errorInfo: React.ErrorInfo): void {
    logger.error("🗺️ LazyMapContainer: Error loading map:", error, errorInfo);
  }

  handleRetry = (): void => {
    // React.lazy retains a rejected import; reload fetches the deployed asset graph again.
    window.location.reload();
  };

  render(): React.ReactNode {
    if (this.state.hasError) {
      return (
        this.props.fallback || (
          <MapErrorFallback
            error={this.state.error || undefined}
            retry={this.handleRetry}
          />
        )
      );
    }

    return this.props.children;
  }
}

/**
 * LazyMapContainer - Lazy-loaded map component with error boundary
 *
 * @example
 * ```tsx
 * import { LazyMapContainer } from './components/Map';
 *
 * function App() {
 *   return (
 *     <LazyMapContainer
 *       mapData={data}
 *       centerLocation={center}
 *       onMapReady={(map, view) => logger.log('Map ready')}
 *     />
 *   );
 * }
 * ```
 */
const LazyMapContainer: React.FC<LazyMapContainerProps> = memo(
  ({
    mapData,
    centerLocation,
    className = "",
    onMapReady,
    onFeatureClick,
    activeTrucks = [],
    onSimulateReportedFire,
  }) => {
    return (
      <MapErrorBoundary>
        <Suspense fallback={<MapLoadingFallback />}>
          <MapComponent
            mapData={mapData}
            centerLocation={centerLocation}
            className={className}
            onMapReady={onMapReady}
            onFeatureClick={onFeatureClick}
            activeTrucks={activeTrucks}
            onSimulateReportedFire={onSimulateReportedFire}
          />
        </Suspense>
      </MapErrorBoundary>
    );
  }
);

LazyMapContainer.displayName = "LazyMapContainer";

export { LazyMapContainer,MapErrorBoundary,MapErrorFallback,MapLoadingFallback };
export default LazyMapContainer;
