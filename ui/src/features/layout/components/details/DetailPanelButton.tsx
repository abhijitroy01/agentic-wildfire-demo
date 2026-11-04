/**
 * DetailPanelButton - FAB that opens the layout detail panel.
 *
 * MIGRATED: Now uses FloatingActionButton primitive.
 * Still content-aware (computes badge from citation/audio/forecast counts)
 * because that's feature logic, not panel logic.
 */
import { FloatingActionButton } from "@/shared/components/panels";
import type React from "react";
import { memo } from "react";

interface DetailPanelButtonProps {
  citationCount?: number;
  audioCount?: number;
  forecastDays?: number;
  weatherCount?: number;
  onClick?: () => void;
  isOpen?: boolean;
}

const DetailPanelButton: React.FC<DetailPanelButtonProps> = memo(
  ({
    citationCount = 0,
    audioCount = 0,
    forecastDays = 0,
    weatherCount = 0,
    onClick,
    isOpen,
  }) => {
    const totalItems =
      citationCount + audioCount + (forecastDays > 0 ? 1 : 0) + weatherCount;

    if (totalItems === 0 || isOpen) {
      return null;
    }

    // Determine icon and badge based on priority
    let icon = "📋";
    let badge: string | number = "";
    const sourcesOnly = citationCount > 0 && audioCount === 0 && forecastDays === 0 && weatherCount === 0;

    if (sourcesOnly) {
      icon = "📚";
      badge = citationCount;
    } else if (totalItems === 1) {
      if (citationCount > 0) {
        icon = "📚";
        badge = citationCount;
      } else if (audioCount > 0) {
        icon = "🔊";
        badge = audioCount;
      } else if (forecastDays > 0) {
        icon = "☁️";
        badge = forecastDays;
      }
    } else {
      icon = "📋";
      badge = "•";
    }

    return (
      <FloatingActionButton
        onClick={onClick || (() => {})}
        icon={icon}
        badge={badge}
        position="bottom-right"
        aria-label={sourcesOnly ? "Open sources panel" : "Open details panel"}
        pulseBadge={totalItems > 1}
      />
    );
  }
);

DetailPanelButton.displayName = "DetailPanelButton";

export default DetailPanelButton;
