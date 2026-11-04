/**
 * Reusable Metadata Display Component
 * Renders flexible metadata for any content type
 */
import type React from "react";
import { memo } from "react";
import "./MetadataDisplay.css";

type MetadataVariant = "default" | "compact" | "detailed";

interface MetadataDisplayProps {
  metadata?: Record<string, string | number | null | undefined>;
  variant?: MetadataVariant;
}

type MetadataKey =
  | "location"
  | "source"
  | "provider"
  | "generatedAt"
  | "lastUpdated"
  | "timestamp"
  | "messageType"
  | "totalItems"
  | "status";

const MetadataDisplay: React.FC<MetadataDisplayProps> = memo(
  ({ metadata, variant = "default" }) => {
    if (!metadata || Object.keys(metadata).length === 0) {
      return null;
    }

    // Icon mapping for common metadata fields
    const getIcon = (key: string): string => {
      const iconMap: Record<MetadataKey, string> = {
        location: "📍",
        source: "🔗",
        provider: "🔗",
        generatedAt: "🕐",
        lastUpdated: "📅",
        timestamp: "🕐",
        messageType: "📢",
        totalItems: "📊",
        status: "✓",
      };
      return iconMap[key as MetadataKey] || "•";
    };

    // Format field names
    const formatKey = (key: string): string => {
      return key
        .replace(/([A-Z])/g, " $1")
        .replace(/^./, (str) => str.toUpperCase())
        .trim();
    };

    // Format date values
    const formatValue = (
      key: string,
      value: string | number | null | undefined
    ): string | number => {
      if (value === null || value === undefined) return "";

      // Date fields that need formatting
      const dateFields = [
        "generatedAt",
        "timestamp",
        "lastUpdated",
        "expiresAt",
      ];

      if (dateFields.includes(key) && typeof value === "string") {
        try {
          // CRITICAL FIX: Clean up invalid ISO format "+00:00Z"
          // Backend mistakenly adds Z after timezone offset
          // Valid: "2025-10-14T22:45:37+00:00" OR "2025-10-14T22:45:37Z"
          // Invalid: "2025-10-14T22:45:37+00:00Z" (what backend sends)
          const cleanedValue = value.replace(/\+00:00Z$/, "+00:00");

          const date = new Date(cleanedValue);
          // Check if date is valid
          if (!isNaN(date.getTime())) {
            return date.toLocaleString();
          }
        } catch {
          // If parsing fails, return original value
          return value;
        }
      }

      return value;
    };

    // Prioritized fields to show first
    const priorityFields = [
      "location",
      "source",
      "provider",
      "generatedAt",
      "timestamp",
    ];
    const sortedEntries = Object.entries(metadata).sort(([keyA], [keyB]) => {
      const indexA = priorityFields.indexOf(keyA);
      const indexB = priorityFields.indexOf(keyB);
      if (indexA === -1 && indexB === -1) return 0;
      if (indexA === -1) return 1;
      if (indexB === -1) return -1;
      return indexA - indexB;
    });

    return (
      <div className={`metadata-display ${variant}`}>
        {sortedEntries.map(([key, value]) => {
          if (!value) return null;

          return (
            <div key={key} className="metadata-item">
              <span className="metadata-icon">{getIcon(key)}</span>
              <span className="metadata-value">
                {key === "location" || key === "source" || key === "provider"
                  ? value
                  : `${formatKey(key)}: ${formatValue(key, value)}`}
              </span>
            </div>
          );
        })}
      </div>
    );
  }
);

MetadataDisplay.displayName = "MetadataDisplay";

export default MetadataDisplay;
