/**
 * SensorDataPanel Component
 * Generic, reusable component for displaying sensor metrics
 *
 * Architecture: Open/Closed Principle
 * - Renders ANY sensor metric passed to it
 * - Adding new sensors doesn't require modifying this component
 * - Configuration-driven display from SensorMetric[] array
 */

import type React from "react";
import type { SensorMetric } from "../types";
import "./SensorDataPanel.css";

/**
 * Props for SensorDataPanel component
 */
export interface SensorDataPanelProps {
  /** Array of sensor metrics to display */
  metrics: SensorMetric[];
  /** Optional title for the panel */
  title?: string;
  /** Whether to show unavailable sensors (value = null) */
  showUnavailable?: boolean;
  /** Optional CSS class for customization */
  className?: string;
}

/**
 * Format sensor value with unit for display
 */
function formatSensorValue(metric: SensorMetric): string {
  if (metric.value === null || metric.value === undefined) {
    return "N/A";
  }

  // Handle string values (like GPS coordinates or wind with direction)
  if (typeof metric.value === "string") {
    return metric.unit ? `${metric.value}${metric.unit}` : metric.value;
  }

  // Handle numeric values
  return metric.unit ? `${metric.value}${metric.unit}` : String(metric.value);
}

/**
 * Get CSS class based on metric status
 */
function getStatusClass(status?: "normal" | "warning" | "critical"): string {
  switch (status) {
    case "warning":
      return "sensor-metric--warning";
    case "critical":
      return "sensor-metric--critical";
    default:
      return "";
  }
}

/**
 * SensorDataPanel - Displays a grid of sensor metrics
 *
 * Features:
 * - Generic: Renders any SensorMetric[] array
 * - Responsive: Grid layout adapts to container
 * - Status indicators: Color-coded based on metric.status
 * - Graceful degradation: Shows N/A for unavailable sensors
 *
 * Usage:
 * ```tsx
 * <SensorDataPanel
 *   metrics={snapshot.sensors}
 *   title="Environmental Data"
 *   showUnavailable={false}
 * />
 * ```
 */
const SensorDataPanel: React.FC<SensorDataPanelProps> = ({
  metrics,
  title = "Sensor Data",
  showUnavailable = true,
  className = "",
}) => {
  // Filter out unavailable sensors if requested
  const displayMetrics = showUnavailable
    ? metrics
    : metrics.filter((m) => m.value !== null && m.value !== undefined);

  // Don't render if no metrics to display
  if (displayMetrics.length === 0) {
    return null;
  }

  return (
    <div className={`sensor-data-panel ${className}`.trim()}>
      {title && <h4 className="sensor-data-panel__title">{title}</h4>}
      <div className="sensor-data-panel__grid">
        {displayMetrics.map((metric) => (
          <div
            key={metric.key}
            className={`sensor-metric ${getStatusClass(metric.status)} ${
              metric.value === null ? "sensor-metric--unavailable" : ""
            }`.trim()}
            title={`${metric.label}: ${formatSensorValue(metric)}`}
          >
            <span className="sensor-metric__icon" aria-hidden="true">
              {metric.icon}
            </span>
            <span className="sensor-metric__label">{metric.label}</span>
            <span className="sensor-metric__value">
              {formatSensorValue(metric)}
            </span>
          </div>
        ))}
      </div>
    </div>
  );
};

export default SensorDataPanel;