/**
 * TruckMapOverlay - Renders fire trucks as CSS markers on top of the map
 * Uses absolute positioning to overlay trucks without touching MapComponent
 */
import {
getRiskColor,getStatusColor,getUnitIcon,getWaterLevelColor,type TruckView,
} from "@/features/firetruck/utils/truckDisplayUtils";
import type MapView from "@arcgis/core/views/MapView";
import type React from "react";
import { memo,useCallback,useEffect,useState } from "react";
import "./TruckMapOverlay.css";

interface TruckDisplay {
  emoji: string;
  color: string;
}

interface TruckWithScreen extends TruckView {
  screenX: number;
  screenY: number;
  display: TruckDisplay;
}

interface TruckMapOverlayProps {
  trucks?: TruckView[];
  mapView?: MapView | null;
}

/**
 * Get truck display with unit type icon and status color
 */
function getTruckDisplay(unitType?: string, status?: string): TruckDisplay {
  // Use unit type for icon (from truckDisplayUtils)
  const emoji = getUnitIcon(unitType);
  
  // Use status for color (from truckDisplayUtils)
  const color = getStatusColor(status);
  
  return { emoji, color };
}

const TruckMapOverlay: React.FC<TruckMapOverlayProps> = memo(
  ({ trucks = [], mapView }) => {
    const [truckPositions, setTruckPositions] = useState<TruckWithScreen[]>([]);
    // Select by unit ID so the popup reads the latest telemetry and screen position, never a click-time copy.
    const [selectedTruckId, setSelectedTruckId] = useState<string | null>(null);
    const selectedTruck = truckPositions.find((truck) => truck.id === selectedTruckId) ?? null;

  // Convert geographic coordinates to screen pixels
  const updatePositions = useCallback(async () => {
    if (!mapView || trucks.length === 0) {
      setTruckPositions([]);
      return;
    }

    // Wait for map to be ready
    if (!mapView.ready) {
      return;
    }

      try {
        // Import Point class for proper geometry
        const Point = await import("@arcgis/core/geometry/Point").then(
          (m) => m.default
        );

        const positions = trucks
          .filter(
            (truck) =>
              truck.position && truck.status?.toUpperCase() !== "CANCELLED"
          )
          .map((truck) => {
            try {
              if (!truck.position) return null;

              // Create proper ArcGIS Point geometry
              const point = new Point({
                longitude: truck.position.longitude,
                latitude: truck.position.latitude,
                spatialReference: { wkid: 4326 },
              });

              // Convert to screen coordinates
              const screenPoint = mapView.toScreen(point);

              if (!screenPoint) {
                return null;
              }

              return {
                ...truck,
                screenX: screenPoint.x,
                screenY: screenPoint.y,
                display: getTruckDisplay(truck.unitType, truck.status),
              } as TruckWithScreen;
            } catch {
              // Silently skip trucks that can't be projected
              return null;
            }
          })
          .filter(
            (t): t is TruckWithScreen =>
              t !== null &&
              t.screenX > -50 &&
              t.screenX < mapView.width + 50 &&
              t.screenY > -50 &&
              t.screenY < mapView.height + 50
          );

        setTruckPositions(positions);
      } catch {
        // Silently handle errors - map may not be fully ready
        // Map initialization in progress
      }
    }, [mapView, trucks]);

    // Update positions when map moves or trucks change
    useEffect(() => {
      updatePositions();

      if (!mapView) return;

      // Listen for map view changes (pan, zoom)
      const watchHandle = mapView.watch(["center", "zoom", "extent"], () => {
        updatePositions();
      });

      return () => {
        if (watchHandle) {
          watchHandle.remove();
        }
      };
    }, [mapView, trucks, updatePositions]);

    // Also update on window resize
    useEffect(() => {
      const handleResize = (): void => {
        updatePositions();
      };
      window.addEventListener("resize", handleResize);
      return () => window.removeEventListener("resize", handleResize);
    }, [updatePositions]);

    // Close popup when clicking outside
    const handleOverlayClick = useCallback(
      (e: React.MouseEvent<HTMLDivElement>): void => {
        if ((e.target as HTMLElement).classList.contains("truck-overlay")) {
          setSelectedTruckId(null);
        }
      },
      []
    );

    if (truckPositions.length === 0) return null;

    return (
      <div className="truck-overlay" onClick={handleOverlayClick}>
        {truckPositions.map((truck) => (
          <div
            key={truck.id}
            className={`truck-marker ${truck.status}`}
            style={
              {
                left: truck.screenX,
                top: truck.screenY,
                "--truck-color": truck.display.color,
              } as React.CSSProperties
            }
            onClick={(e): void => {
              e.stopPropagation();
              setSelectedTruckId((current) => (current === truck.id ? null : truck.id));
            }}
          >
            <span className="truck-icon">{truck.display.emoji}</span>
            {/* Show pulse for all active statuses (except CANCELLED) */}
            {truck.status?.toUpperCase() !== "CANCELLED" && (
              <span className="truck-pulse"></span>
            )}
          </div>
        ))}

        {/* Popup for selected truck - shows real telemetry */}
        {selectedTruck && (
          <div
            className="truck-popup"
            style={{
              left: selectedTruck.screenX + 30,
              top: selectedTruck.screenY - 20,
            }}
          >
            <div className="popup-header">
              <span>🚒 {selectedTruck.id}</span>
              <button
                className="popup-close"
                onClick={(): void => setSelectedTruckId(null)}
              >
                ✕
              </button>
            </div>
            <div className="popup-content">
              {/* Status */}
              <div className="popup-row">
                <span className="popup-label">Status:</span>
                <span
                  className="popup-value status"
                  style={{ color: selectedTruck.display.color }}
                >
                  {selectedTruck.status?.replace(/_/g, " ")}
                </span>
              </div>

              {/* Speed & Heading */}
              <div className="popup-row">
                <span className="popup-label">🚗 Speed:</span>
                <span className="popup-value">
                  {Math.round(selectedTruck.speedMph || 0)} mph
                </span>
              </div>
              <div className="popup-row">
                <span className="popup-label">🧭 Heading:</span>
                <span className="popup-value">
                  {Math.round(selectedTruck.heading || 0)}°
                </span>
              </div>

              {/* Water Level */}
              <div className="popup-row">
                <span className="popup-label">💧 Water:</span>
                <span className="popup-value">
                  {selectedTruck.waterLevelPct || 0}%
                </span>
              </div>
              <div className="popup-progress-bar">
                <div
                  className="popup-progress-fill"
                  style={{
                    width: `${selectedTruck.waterLevelPct || 0}%`,
                    backgroundColor: getWaterLevelColor(selectedTruck.waterLevelPct || 0),
                  }}
                />
              </div>

              {/* Crew Info */}
              <div className="popup-row">
                <span className="popup-label">👥 Crew:</span>
                <span className="popup-value">
                  {selectedTruck.crewCount || 0} personnel
                </span>
              </div>
              <div className="popup-row">
                <span className="popup-label">⏱️ On Duty:</span>
                <span className="popup-value">
                  {(selectedTruck.hoursOnDuty || 0).toFixed(1)}h
                </span>
              </div>

              {/* Safety */}
              <div className="popup-row">
                <span className="popup-label">⚠️ Risk:</span>
                <span
                  className="popup-value"
                  style={{ color: getRiskColor(selectedTruck.entrapmentRisk || "LOW") }}
                >
                  {selectedTruck.entrapmentRisk || "LOW"}
                </span>
              </div>
              {selectedTruck.inDangerZone && (
                <div className="popup-danger-warning">🔴 IN DANGER ZONE</div>
              )}
            </div>
          </div>
        )}
      </div>
    );
  }
);

TruckMapOverlay.displayName = "TruckMapOverlay";

export default TruckMapOverlay;
