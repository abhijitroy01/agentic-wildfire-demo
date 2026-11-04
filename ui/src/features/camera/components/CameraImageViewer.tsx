/**
 * CameraImageViewer Component
 * Displays camera feed image with bounding box overlays for fire/smoke detections
 */

import { logger } from "@/shared/utils/logger";
import type React from "react";
import { useCallback,useEffect,useRef,useState } from "react";
import type { BoundingBox,CameraImageViewerProps } from "../types";

/**
 * Draw a bounding box with label on the canvas
 */
function drawBoundingBox(
  ctx: CanvasRenderingContext2D,
  bbox: BoundingBox,
  className: string,
  confidence: number,
  scale: number
): void {
  // API returns center coordinates, convert to top-left
  const x = (bbox.x - bbox.width / 2) * scale;
  const y = (bbox.y - bbox.height / 2) * scale;
  const width = bbox.width * scale;
  const height = bbox.height * scale;

  // Color by detection type
  const color = className.toLowerCase() === "fire" ? "#DC2626" : "#EA580C";

  // Draw rectangle
  ctx.strokeStyle = color;
  ctx.lineWidth = 3;
  ctx.strokeRect(x, y, width, height);

  // Draw label background
  const label = `${className.toUpperCase()} ${(confidence * 100).toFixed(1)}%`;
  ctx.font = "bold 14px Arial";
  const textWidth = ctx.measureText(label).width;
  ctx.fillStyle = color;
  ctx.fillRect(x, y - 22, textWidth + 8, 20);

  // Draw label text
  ctx.fillStyle = "white";
  ctx.fillText(label, x + 4, y - 7);
}

const CameraImageViewer: React.FC<CameraImageViewerProps> = ({
  imageUrl,
  detection,
  isLoading = false,
  error = null,
}) => {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const imageRef = useRef<HTMLImageElement | null>(null);
  const [imageError, setImageError] = useState<string | null>(null);

  const renderCanvas = useCallback(() => {
    const canvas = canvasRef.current;
    const image = imageRef.current;

    if (!canvas || !image || !image.complete) return;

    const ctx = canvas.getContext("2d");
    if (!ctx) return;

    // Set canvas size to match container while maintaining aspect ratio
    const containerWidth = canvas.parentElement?.clientWidth || 800;
    const maxHeight = 500;

    const aspectRatio = image.naturalWidth / image.naturalHeight;
    let displayWidth = containerWidth;
    let displayHeight = containerWidth / aspectRatio;

    if (displayHeight > maxHeight) {
      displayHeight = maxHeight;
      displayWidth = maxHeight * aspectRatio;
    }

    canvas.width = displayWidth;
    canvas.height = displayHeight;

    // Draw the image
    ctx.drawImage(image, 0, 0, displayWidth, displayHeight);

    // Draw bounding boxes if detection data exists
    if (detection && detection.boundingBoxes.length > 0) {
      const scaleX = displayWidth / image.naturalWidth;
      const scaleY = displayHeight / image.naturalHeight;
      const scale = Math.min(scaleX, scaleY);

      detection.boundingBoxes.forEach((bbox, index) => {
        const className = detection.classes[index] || "unknown";
        const confidence = detection.confidences[index] || 0;
        drawBoundingBox(ctx, bbox, className, confidence, scale);
      });
    }
  }, [detection]);

  useEffect(() => {
    setImageError(null);
    if (!imageUrl) return;

    const image = new Image();
    let active = true;
    image.crossOrigin = "anonymous";

    image.onload = () => {
      if (!active) return;
      imageRef.current = image;
      renderCanvas();
    };

    image.onerror = () => {
      if (!active) return;
      logger.error("[CameraImageViewer] Failed to load image");
      imageRef.current = null;
      setImageError("The camera image could not be decoded. Refresh to request a new snapshot.");
    };

    image.src = imageUrl;

    return () => {
      active = false;
      image.onload = null;
      image.onerror = null;
      imageRef.current = null;
    };
  }, [imageUrl, renderCanvas]);

  // Re-render on window resize
  useEffect(() => {
    const handleResize = () : void => {
      renderCanvas();
    };

    window.addEventListener("resize", handleResize);
    return () => window.removeEventListener("resize", handleResize);
  }, [renderCanvas]);

  if (isLoading) {
    return (
      <div className="camera-image-viewer camera-image-viewer--loading">
        <div className="camera-image-viewer__spinner">
          <div className="spinner"></div>
          <span>Loading camera feed...</span>
        </div>
      </div>
    );
  }

  if (error || imageError) {
    return (
      <div className="camera-image-viewer camera-image-viewer--error">
        <div className="camera-image-viewer__error">
          <span className="error-icon">⚠️</span>
          <span className="error-message">{error || imageError}</span>
        </div>
      </div>
    );
  }

  if (!imageUrl) {
    return (
      <div className="camera-image-viewer camera-image-viewer--empty">
        <div className="camera-image-viewer__placeholder">
          <span className="placeholder-icon">📷</span>
          <span className="placeholder-text">No image available</span>
        </div>
      </div>
    );
  }

  return (
    <div className="camera-image-viewer">
      <canvas
        ref={canvasRef}
        className="camera-image-viewer__canvas"
        role="img"
        aria-label="Camera feed with detection overlays"
      />
    </div>
  );
};

export default CameraImageViewer;
