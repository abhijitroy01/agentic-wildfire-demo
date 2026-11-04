/**
 * DetailPanel - Layout-owned composition for citations, voice messages, and weather data.
 *
 * MIGRATED: Now uses DockedPanel primitive for positioning, animation, z-index, ESC, ARIA.
 * This component only handles content composition.
 */
import CitationsSection from "@/features/chat/components/CitationsSection";
import AudioSection from "@/features/radio-monitor/components/AudioSection";
import type { ForecastData } from "@/features/weather";
import ForecastSection from "@/features/weather/components/ForecastSection";
import CurrentWeatherSection from "@/features/weather/components/CurrentWeatherSection";
import type { ParsedAudioData, KnowledgeBaseCitation, LocationData } from "@/shared/services/agentResponseParser";
import { INCIDENT_TIME_ZONE } from "@/shared/domain/incidentObservations";
import { DockedPanel } from "@/shared/components/panels";
import type React from "react";
import { memo,useState } from "react";

// --- Type Definitions ---

interface MetadataType {
  [key: string]: string | number | null | undefined;
}

interface DetailPanelProps {
  citations?: KnowledgeBaseCitation[] | null;
  audioData?: ParsedAudioData | null;
  locations?: LocationData[];
  forecastData?: ForecastData | null;
  citationMetadata?: MetadataType | null;
  audioMetadata?: MetadataType | null;
  forecastMetadata?: MetadataType | null;
  isOpen: boolean;
  onClose: () => void;
}

interface ExpandedSections {
  citations: boolean;
  audio: boolean;
}

const DetailPanel: React.FC<DetailPanelProps> = memo(
  ({
    citations,
    audioData,
    forecastData,
    locations = [],
    citationMetadata,
    audioMetadata,
    forecastMetadata,
    isOpen,
    onClose,
  }) => {
    const [expandedSections, setExpandedSections] = useState<ExpandedSections>({
      citations: true,
      audio: true,
    });

    // Determine what content we have
    const hasCitations = citations && citations.length > 0;
    const hasAudio = audioData?.languages;
    const hasForecast = forecastData?.days;
    const hasWeather = locations.some(item => item.currentWeather);
    const contentCount = [hasCitations, hasAudio, hasForecast, hasWeather].filter(
      Boolean
    ).length;

    // Determine panel heading
    const getPanelHeading = (): string => {
      if (contentCount === 1) {
        if (hasCitations) return `Sources (${citations!.length})`;
        if (hasAudio)
          return `Voice drafts (${audioData!.summary?.totalLanguages || 0})`;
        if (hasForecast)
          return `Weather Forecast (${forecastData!.days?.length || 0} days)`;
        if (hasWeather) return "Weather observations";
      }
      return "Details";
    };

    const getPanelIcon = (): string => {
      if (contentCount === 1) {
        if (hasCitations) return "📚";
        if (hasAudio) return "🔊";
        if (hasForecast) return "☁️";
      }
      return "📋";
    };

    const toggleSection = (section: keyof ExpandedSections): void => {
      setExpandedSections((prev) => ({
        ...prev,
        [section]: !prev[section],
      }));
    };

    return (
      <DockedPanel
        id="detail-panel"
        isOpen={isOpen}
        onClose={onClose}
        title={getPanelHeading()}
        icon={getPanelIcon()}
        width={420}
        className="detail-panel-wrapper"
      >
        {/* Citations Section */}
        <CurrentWeatherSection locations={locations} />
        {hasCitations && (
          <CitationsSection
            citations={citations!}
            metadata={citationMetadata}
            usesAccordion={contentCount > 1}
            isExpanded={expandedSections.citations}
            onToggle={() => toggleSection("citations")}
          />
        )}

        {/* Audio Section */}
        {hasAudio && (
          <AudioSection
            audioData={audioData!}
            metadata={audioMetadata}
            usesAccordion={contentCount > 1}
            isExpanded={expandedSections.audio}
            onToggle={() => toggleSection("audio")}
          />
        )}

        {/* Forecast Section */}
        {hasForecast && (
          <ForecastSection
            forecastData={forecastData!}
            metadata={forecastMetadata}
            usesAccordion={contentCount > 1}
            timeZone={INCIDENT_TIME_ZONE}
          />
        )}
      </DockedPanel>
    );
  }
);

DetailPanel.displayName = "DetailPanel";

export default DetailPanel;
