/**
 * TruckCard - Individual truck status display card
 */
import {
formatStatus,
getRiskColor,
getStatusColor,
getUnitIcon,
getWaterLevelColor,
type TruckView,
} from "@/features/firetruck/utils/truckDisplayUtils";
import type React from "react";
import { memo } from "react";

interface TruckCardProps {
  truck: TruckView;
}

const TruckCard: React.FC<TruckCardProps> = memo(({ truck }) => {
  return (
    <div className={`rdp-truck-card ${truck.inDangerZone ? "danger" : ""}`}>
      <div className="rdp-truck-header">
        <span className="rdp-truck-icon">{getUnitIcon(truck.unitType)}</span>
        <span className="rdp-truck-name">{truck.id}</span>
        <span
          className="rdp-truck-status"
          style={{ backgroundColor: getStatusColor(truck.status) }}
        >
          {formatStatus(truck.status)}
        </span>
      </div>

      {/* Telemetry Row */}
      <div className="rdp-truck-telemetry">
        <span title="Speed">🚗 {Math.round(truck.speedMph)} mph</span>
        <span title="Heading">🧭 {Math.round(truck.heading)}°</span>
        <span title="Crew">👥 {truck.crewCount}</span>
      </div>

      {/* Water Level */}
      <div className="rdp-water-section">
        <div className="rdp-water-label">
          <span>💧 Water</span>
          <span>{truck.waterLevelPct}%</span>
        </div>
        <div className="rdp-water-bar">
          <div
            className="rdp-water-fill"
            style={{
              width: `${truck.waterLevelPct}%`,
              backgroundColor: getWaterLevelColor(truck.waterLevelPct),
            }}
          />
        </div>
        {truck.pumpActive && (
          <span className="rdp-pump-active">
            ⚡ Pumping {truck.flowRateGpm} GPM
          </span>
        )}
      </div>

      {/* Safety Indicators */}
      <div className="rdp-safety-row">
        {truck.inDangerZone && (
          <span className="rdp-danger-badge">⚠️ DANGER ZONE</span>
        )}
        <span
          className="rdp-risk-badge"
          style={{ backgroundColor: getRiskColor(truck.entrapmentRisk) }}
        >
          Risk: {truck.entrapmentRisk}
        </span>
        <span className="rdp-hours">{truck.hoursOnDuty?.toFixed(1)}h on duty</span>
      </div>
    </div>
  );
});

TruckCard.displayName = "TruckCard";

export default TruckCard;
