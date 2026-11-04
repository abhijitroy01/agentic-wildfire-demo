/**
 * CameraFeedModal Component
 * Modal overlay for displaying WildfireAI camera feed with detection and sensor data
 *
 * MIGRATED: Now uses ModalPanel primitive for backdrop, focus trap, ESC, scroll lock.
 * This component only handles camera-specific content rendering.
 */

import { ModalPanel } from "@/shared/components/panels";
import type React from "react";
import { memo } from "react";
import { useCameraData } from "../hooks/useCameraData";
import type { CameraFeedModalProps } from "../types";
import "./CameraFeedModal.css";
import CameraImageViewer from "./CameraImageViewer";
import SensorDataPanel from "./SensorDataPanel";

const CameraFeedModal: React.FC<CameraFeedModalProps> = memo(
  ({ isOpen, onClose, fireName }) => {
    // All state management handled by custom hook
    const { snapshot, isLoading, error, refresh, config } =
      useCameraData(isOpen);

    const detection = snapshot?.detection;

    const formatTimestamp = (timestamp: string): string => {
      try {
        return new Date(timestamp).toLocaleString();
      } catch {
        return timestamp;
      }
    };

    const getConfidenceClass = (prob: number): string => {
      if (prob >= 0.7) return "confidence--high";
      if (prob >= 0.5) return "confidence--medium";
      if (prob >= 0.3) return "confidence--low";
      return "confidence--none";
    };

    const modalFooter = (
      <>
        <button
          className="camera-feed-modal__refresh"
          onClick={refresh}
          disabled={isLoading}
        >
          🔄 Refresh
        </button>
        <button className="camera-feed-modal__close-btn" onClick={onClose}>
          Close
        </button>
      </>
    );

    return (
      <ModalPanel
        id="camera-modal"
        isOpen={isOpen}
        onClose={onClose}
        title={`📷 Camera Feed${fireName ? ` - ${fireName}` : ""}`}
        maxWidth="900px"
        footer={modalFooter}
      >
        {/* Camera meta info */}
        <div className="camera-feed-modal__meta">
          <span className="camera-feed-modal__device">
            Device: {config.deviceId || "—"}
          </span>
          <span className="camera-feed-modal__sensor">
            Sensor: {config.sensorAlias || "—"}
          </span>
        </div>
        {/* The title names the clicked fire; the camera itself is one configured device. */}
        <p className="camera-feed-modal__caption">
          One fixed demonstration camera; it is not located at the selected map marker.
        </p>

        {/* Content */}
        <div className="camera-feed-modal__content">
          {/* Image Viewer */}
          <div className="camera-feed-modal__image-container">
            <CameraImageViewer
              imageUrl={snapshot?.imageUrl || null}
              detection={detection?.detection || null}
              isLoading={isLoading}
              error={error}
            />
          </div>

          {/* Detection Info */}
          {detection && !isLoading && !error && (
            <div className="camera-feed-modal__info">
              <div className="camera-feed-modal__timestamp">
                <strong>Captured:</strong>{" "}
                {formatTimestamp(detection.timeRecorded)}
              </div>

              <div className="camera-feed-modal__probabilities">
                <div
                  className={`camera-feed-modal__probability ${getConfidenceClass(detection.smokeProb)}`}
                >
                  <span className="probability-label">🌫️ Smoke</span>
                  <span className="probability-value">
                    {(detection.smokeProb * 100).toFixed(1)}%
                  </span>
                </div>
                <div
                  className={`camera-feed-modal__probability ${getConfidenceClass(detection.fireProb)}`}
                >
                  <span className="probability-label">🔥 Fire</span>
                  <span className="probability-value">
                    {(detection.fireProb * 100).toFixed(1)}%
                  </span>
                </div>
              </div>

              {detection.detection &&
                detection.detection.classes.length > 0 && (
                  <div className="camera-feed-modal__detections">
                    <strong>Detections:</strong>
                    <ul className="detection-list">
                      {detection.detection.classes.map(
                        (cls: string, idx: number) => (
                          <li key={idx} className="detection-item">
                            <span className="detection-class">{cls}</span>
                            <span className="detection-confidence">
                              {(
                                detection.detection!.confidences[idx] * 100
                              ).toFixed(1)}
                              %
                            </span>
                          </li>
                        )
                      )}
                    </ul>
                  </div>
                )}
            </div>
          )}

          {/* Sensor Data Panel */}
          {snapshot && snapshot.sensors.length > 0 && !isLoading && (
            <SensorDataPanel
              metrics={snapshot.sensors}
              title="Environmental Sensors"
              showUnavailable={true}
            />
          )}
        </div>
      </ModalPanel>
    );
  }
);

CameraFeedModal.displayName = "CameraFeedModal";

export default CameraFeedModal;