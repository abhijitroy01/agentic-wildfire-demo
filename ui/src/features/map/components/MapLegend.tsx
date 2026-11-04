/**
 * MapLegend - Extracted legend component for the map
 */
import { STATUS_DISPLAY,UNIT_DISPLAY } from "@/features/firetruck/utils/truckDisplayUtils";
import Accordion from "@/shared/components/Accordion";
import { WIND_COLORS } from "@/shared/constants";
import type React from "react";
import { memo, useState } from "react";
import { scenarioContourStyle } from "../utils/fireExposureGraphics";
import "./MapLegend.css";

interface MapLegendProps {
  showWind?: boolean;
  showFireExposure?: boolean;
  showSurfaceFire?: boolean;
  showLandscape?: boolean;
  showObservedPerimeter?: boolean;
  landscapeMinute?: number;
  landscapeHorizons?: readonly number[];
  surfaceFireMinute?: number;
  surfaceFireHorizons?: readonly number[];
  showResponseUnits?: boolean;
}

/** Keys use the same full returned time sequence as the renderer, including times beyond the selected step. */
function ScenarioContoursLegend({ title, minute, horizons }: {
  title: string; minute?: number; horizons: readonly number[];
}): React.ReactElement {
  const visibleHorizons = horizons.filter(value => minute !== undefined && value <= minute);
  return <div className="scenario-legend">
    <p className="scenario-legend-title">{title}{minute === undefined ? "" : ` · ${minute} min`}</p>
    {visibleHorizons.map(value => {
      const { outline, fill } = scenarioContourStyle(value, horizons);
      return <div className="legend-item" key={value}>
        <span className="legend-icon scenario-contour-icon" style={{
          borderColor: `rgb(${outline.slice(0, 3).join(" ")})`,
          backgroundColor: `rgb(${fill.slice(0, 3).join(" ")} / ${fill[3]})`,
        }} />
        <span>Reached by {value} min</span>
      </div>;
    })}
    <p className="scenario-legend-note">Minutes after assumed ignition.</p>
  </div>;
}

/** Unit types and status ring colors, folded into one compact "Fire response units" toggle until opened. */
function ResponseUnitsLegend(): React.ReactElement {
  const [expanded, setExpanded] = useState(false);
  return (
    <div className="response-units-legend">
      <Accordion title="Fire response units" isExpanded={expanded} onToggle={() => setExpanded(open => !open)}>
        {/* Unit Types */}
        <div className="legend-section">
          <h5>Unit Types</h5>
          <div className="legend-items">
            {Object.values(UNIT_DISPLAY).map(({ icon, name }) => (
              <div className="legend-item" key={name}>
                <span className="unit-icon">{icon}</span>
                <span>{name}</span>
              </div>
            ))}
          </div>
        </div>

        {/* Status Colors */}
        <div className="legend-section">
          <h5>Status (Ring Color)</h5>
          <div className="legend-items">
            {Object.values(STATUS_DISPLAY).map(({ color, label }) => (
              <div className="legend-item" key={label}>
                <div className="status-indicator" style={{ backgroundColor: color }}></div>
                <span>{label}</span>
              </div>
            ))}
          </div>
        </div>
      </Accordion>
    </div>
  );
}

const MapLegend: React.FC<MapLegendProps> = memo(
  ({ showWind = false, showFireExposure = false, showSurfaceFire = false, showLandscape = false,
    showObservedPerimeter = false, landscapeMinute, landscapeHorizons = [],
    surfaceFireMinute, surfaceFireHorizons = [], showResponseUnits = false }) => {
    const [windExpanded, setWindExpanded] = useState(false);
    return (
      <>
        {/* Main Legend */}
        <div className="map-legend">
          <h4>Legend</h4>
          <div className="legend-item">
            <div className="legend-icon location-icon"></div>
            <span>Locations</span>
          </div>
          <div className="legend-item">
            <div className="legend-icon fire-icon"></div>
            <span>Reported fire location</span>
          </div>
          {showObservedPerimeter && <div className="legend-item">
            <span className="legend-icon reported-perimeter-icon" />
            <span>Reported perimeter</span>
          </div>}
          {showFireExposure && <div className="legend-item">
            <div className="legend-icon exposure-icon"></div>
            <span>Illustrative exposure</span>
          </div>}
          {showSurfaceFire && <ScenarioContoursLegend title="Uniform-fuel spread"
            minute={surfaceFireMinute} horizons={surfaceFireHorizons} />}
          {showLandscape && <ScenarioContoursLegend title="Modeled spread"
            minute={landscapeMinute} horizons={landscapeHorizons} />}
        </div>

        {/* Wind Legend */}
        {showWind && (
          <div className="wind-legend">
            <Accordion title="Wind speed" isExpanded={windExpanded}
              onToggle={() => setWindExpanded(open => !open)}>
            <div className="wind-speed-scale">
              <div className="wind-item">
                <div
                  className="wind-arrow"
                  style={{
                    borderBottomColor: `rgba(${WIND_COLORS.light.join(",")})`,
                  }}
                ></div>
                <span>0-6 mph (Light)</span>
              </div>
              <div className="wind-item">
                <div
                  className="wind-arrow"
                  style={{
                    borderBottomColor: `rgba(${WIND_COLORS.moderate.join(",")})`,
                  }}
                ></div>
                <span>6-12 mph (Moderate)</span>
              </div>
              <div className="wind-item">
                <div
                  className="wind-arrow"
                  style={{
                    borderBottomColor: `rgba(${WIND_COLORS.strong.join(",")})`,
                  }}
                ></div>
                <span>12-19 mph (Strong)</span>
              </div>
              <div className="wind-item">
                <div
                  className="wind-arrow"
                  style={{
                    borderBottomColor: `rgba(${WIND_COLORS.veryStrong.join(",")})`,
                  }}
                ></div>
                <span>19-31 mph (Very Strong)</span>
              </div>
              <div className="wind-item">
                <div
                  className="wind-arrow"
                  style={{
                    borderBottomColor: `rgba(${WIND_COLORS.extreme.join(",")})`,
                  }}
                ></div>
                <span>31+ mph (Extreme)</span>
              </div>
            </div>
            <div className="wind-legend-footer">
              <div>🧭 Arrow points in wind direction</div>
              <div>📏 Size indicates wind speed</div>
            </div>
            </Accordion>
          </div>
        )}

        {/* Fire Response Units Legend, shown only while the map has units */}
        {showResponseUnits && <ResponseUnitsLegend />}
      </>
    );
  }
);

MapLegend.displayName = "MapLegend";

export default MapLegend;
