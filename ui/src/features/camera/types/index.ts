/**
 * Camera Feature Types
 * TypeScript interfaces for WildfireAI camera integration
 */

export interface BoundingBox {
  x: number; // x-center in pixels
  y: number; // y-center in pixels
  width: number; // width in pixels
  height: number; // height in pixels
}

export interface Detection {
  classes: string[];
  confidences: number[];
  boundingBoxes: BoundingBox[];
}

export interface Evidence {
  id: string;
  type: string;
  image?: {
    filename: string;
  };
}

export interface CameraDetection {
  smokeProb: number;
  fireProb: number;
  detection: Detection | null;
  evidenceId: string | null;
  timeRecorded: string;
  sensorAlias: string;
  deviceId: string;
}

export interface CameraFeedModalProps {
  isOpen: boolean;
  onClose: () => void;
  fireName?: string;
}

export interface CameraImageViewerProps {
  imageUrl: string | null;
  detection: Detection | null;
  isLoading?: boolean;
  error?: string | null;
}

// API Response Types - Matches actual WildfireAI /latest response (the broker keeps the same shape, fewer fields)
export interface SensorRecord {
  id?: string; // Record ID - used as image ID for /images/{id} endpoint
  type?: string; // e.g., "image", "event", "gps", "gas", "anemometer", "voc"
  timeRecorded?: string; // image records carry it; the broker's sensor records omit it
  sensorNo?: number;
  deviceId?: string;

  // Camera/Image specific
  image?: {
    filename: string;
    isInfrared: boolean;
    orientationRecord: unknown;
  };
  claims?: Claim[];
  detection?: {
    classes: string[];
    confidences: number[];
    boundingBoxes: number[][];
  };
  evidences?: Evidence[];

  // Device-level sensor data (from navigation, gas, wind, air_quality groups)
  gps?: {
    latitude: number;
    longitude: number;
    altitude: number | null;
    quality: number | null;
    numberOfSatellites: number | null;
    HDOP: number | null;
  };
  gas?: {
    gas: number;
    humidity: number;
    pressure: number;
    altitude: number;
  };
  anemometer?: {
    angle: number;
    speed: number;
  };
  voc?: {
    TVOC: number;
    eCO2: number;
    CO2: number;
  };
}

export interface Claim {
  type: string;
  smoke?: {
    smokeProb: number;
  };
  fire?: {
    fireProb: number;
  };
  // Delphire's DetectionRecord: classes with one confidence (0-1) each, and pixel boxes
  // [x_center, y_center, width, height], one per class when present.
  detection?: {
    classes: string[];
    confidences: number[];
    boundingBoxes: number[][];
  };
}

export interface SensorGroup {
  alias?: string; // API response uses 'alias' field
  sensorAlias?: string; // Keeping for backward compatibility
  records: SensorRecord[];
}

// ============================================================================
// Sensor Data Types - Matches Actual WildfireAI API Response
// ============================================================================

/**
 * Generic sensor metric for uniform rendering in SensorDataPanel
 */
export interface SensorMetric {
  key: string;
  icon: string;
  label: string;
  value: string | number | null;
  unit: string;
  status?: "normal" | "warning" | "critical";
}

/**
 * Camera snapshot - unified response from brokerCameraSnapshot()
 * Contains all data needed for the camera modal in a single object
 *
 * Architecture: Single API call (/latest) provides both camera AND sensor data
 */
export interface CameraSnapshot {
  // Core detection data (from camera sensor group)
  detection: CameraDetection | null;
  imageUrl: string | null;

  // Device-level sensor data (extracted from same /latest response)
  sensors: SensorMetric[];

  // Metadata
  timestamp: string;
  deviceId: string;
  sensorAlias: string;
}
