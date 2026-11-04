import MetadataDisplay from "@/features/chat/components/MetadataDisplay";
import Accordion from "@/shared/components/Accordion";
import type React from "react";
import { memo,useEffect,useState } from "react";
import "./AudioSection.css";
import type { components } from "@/schemas/generated/api-types";
import { allowedHttpsUrl, isAmazonS3Host } from "@/shared/utils/urlAllowlist";

// --- Type Definitions ---
// UPDATED: Field names now match AudioLanguage Pydantic schema (camelCase)

/**
 * LanguageData interface - STRICT schema compliance
 * Matches AudioLanguage Pydantic model (backend/runtime/schemas/generated_models.py)
 * 
 * NOTE: Legacy fields (name, url) have been REMOVED to enforce schema compliance.
 * If backend returns legacy fields, it's a bug that should be fixed in the backend.
 */
type LanguageData = components["schemas"]["AudioLanguage"];
type AudioDataType = NonNullable<components["schemas"]["AudioData"]>;

interface AudioMetadata {
  source?: string;
  [key: string]: string | number | null | undefined;
}

interface AudioSectionProps {
  audioData: AudioDataType;
  metadata?: AudioMetadata | null;
  usesAccordion?: boolean;
  isExpanded?: boolean;
  onToggle?: () => void;
}

interface AudioItemProps {
  code: string;
  data: LanguageData;
  compact?: boolean;
  expandedLanguages: Set<string>;
  onToggleLanguage: (code: string) => void;
  copiedUrl: string | null;
  onCopyUrl: (url: string, code: string) => void;
  now: number;
}

// STRICT: Return audioUrl only - no fallbacks to catch schema violations
const getAudioUrl = (data: LanguageData): string | undefined => {
  return allowedHttpsUrl(data.audioUrl, isAmazonS3Host);
};

// STRICT: Return languageName only (fallback to code is acceptable)
const getLanguageName = (data: LanguageData, code: string): string => {
  return data.languageName || code.toUpperCase();
};

const formatExpiration = (expires?: number): string | null => {
  if (!expires) return null;

  const now = Math.floor(Date.now() / 1000);
  const secondsLeft = expires - now;

  if (secondsLeft <= 0) {
    return "🔴 Expired";
  } else if (secondsLeft < 3600) {
    const minutes = Math.floor(secondsLeft / 60);
    return `⏰ Expires in ${minutes}m`;
  } else {
    const hours = Math.floor(secondsLeft / 3600);
    const minutes = Math.floor((secondsLeft % 3600) / 60);
    return `⏰ Expires in ${hours}h ${minutes}m`;
  }
};

const AudioItem: React.FC<AudioItemProps> = memo(
  ({
    code,
    data,
    compact,
    expandedLanguages,
    onToggleLanguage,
    copiedUrl,
    onCopyUrl,
    now,
  }) => {
    const isLanguageExpanded = expandedLanguages.has(code);
    const isExpired = data.isExpired || (data.expires ? data.expires <= now : false);
    const audioUrl = getAudioUrl(data);
    const languageName = getLanguageName(data, code);
    const Header = compact ? "div" : "button";

    return (
      <div
        className={`audio-item ${compact ? "compact" : ""} ${
          isExpired ? "expired" : ""
        }`}
      >
        <Header
          className="audio-header"
          onClick={compact ? undefined : () => onToggleLanguage(code)}
          type={compact ? undefined : "button"}
          aria-expanded={compact ? undefined : isLanguageExpanded}
          style={{ cursor: compact ? "default" : "pointer" }}
        >
          <div className="audio-info">
            <span className="audio-language">
              {languageName}
            </span>
            <span className="audio-voice">
              Voice: {data.voice || "Unknown"}
            </span>
          </div>
          {!compact && (
            <div className="audio-status">
              <span className="audio-expiry">
                {formatExpiration(data.expires)}
              </span>
              <span
                className={`expand-icon ${isLanguageExpanded ? "expanded" : ""}`}
              >
                ▼
              </span>
            </div>
          )}
        </Header>

        {((compact && !isExpired) || isLanguageExpanded) && audioUrl && (
          <div className="audio-content">
            {!isExpired && (
              <audio
                controls
                preload="none"
                aria-label={`${languageName} draft recording`}
                className={`audio-player ${compact ? "compact" : ""}`}
              >
                <source src={audioUrl} type="audio/mpeg" />
                Your browser does not support the audio element.
              </audio>
            )}

            {!compact && isLanguageExpanded && !isExpired && audioUrl && (
              <div className="audio-actions">
                <button
                  className="copy-url-button"
                  onClick={() => onCopyUrl(audioUrl, code)}
                >
                  {copiedUrl === code ? "✅ Copied!" : "📋 Copy URL"}
                </button>
              </div>
            )}

          </div>
        )}
        {data.translatedText && <details><summary>Review message text</summary><p>{data.translatedText}</p></details>}

        {isExpired && (
          <div className="audio-expired">
            ⚠️ Audio link has expired - generate new messages
          </div>
        )}
        
        {!audioUrl && !isExpired && (
          <div className="audio-error">
            ⚠️ Audio URL not available
          </div>
        )}
      </div>
    );
  }
);

AudioItem.displayName = "AudioItem";

const AudioSection: React.FC<AudioSectionProps> = memo(
  ({ audioData, metadata, usesAccordion, isExpanded, onToggle }) => {
    const [expandedLanguages, setExpandedLanguages] = useState<Set<string>>(
      new Set()
    );
    const [copiedUrl, setCopiedUrl] = useState<string | null>(null);
    const [currentTime, setCurrentTime] = useState(() => Math.floor(Date.now() / 1000));
    useEffect(() => {
      const timer = setInterval(() => setCurrentTime(Math.floor(Date.now() / 1000)), 30000);
      return () => clearInterval(timer);
    }, []);

    const toggleLanguage = (code: string): void => {
      setExpandedLanguages((prev) => {
        const next = new Set(prev);
        if (next.has(code)) {
          next.delete(code);
        } else {
          next.add(code);
        }
        return next;
      });
    };

    const copyUrl = (url: string, code: string): void => {
      navigator.clipboard.writeText(url);
      setCopiedUrl(code);
      setTimeout(() => setCopiedUrl(null), 2000);
    };

    const hasExpired = Object.values(audioData.languages || {}).some(
      (lang) => lang.isExpired || (lang.expires && lang.expires < currentTime)
    );

    if (usesAccordion) {
      return (
        <Accordion
          title="Voice drafts for review"
          badge={`${audioData.summary?.totalLanguages || Object.keys(audioData.languages || {}).length || 0}`}
          isExpanded={isExpanded}
          onToggle={onToggle}
          variant="audio"
        >
          {metadata && (
            <MetadataDisplay metadata={metadata} variant="compact" />
          )}
          {hasExpired && (
            <div className="warning-banner-compact">
              ⚠️ Some messages have expired
            </div>
          )}
          <div className="audio-list-compact">
            {Object.entries(audioData.languages || {}).map(([code, data]) => (
              <AudioItem
                key={code}
                code={code}
                data={data}
                compact
                expandedLanguages={expandedLanguages}
                onToggleLanguage={toggleLanguage}
                copiedUrl={copiedUrl}
                onCopyUrl={copyUrl}
                now={currentTime}
              />
            ))}
          </div>
        </Accordion>
      );
    }

    return (
      <div className="detail-section-full">
        {metadata && <MetadataDisplay metadata={metadata} variant="default" />}
        {hasExpired && (
          <div className="warning-banner">
            ⚠️ Some voice messages have expired. Generate new ones if needed.
          </div>
        )}
        <div className="audio-list-full">
          {Object.entries(audioData.languages || {}).map(([code, data]) => (
            <AudioItem
              key={code}
              code={code}
              data={data}
              expandedLanguages={expandedLanguages}
              onToggleLanguage={toggleLanguage}
              copiedUrl={copiedUrl}
              onCopyUrl={copyUrl}
              now={currentTime}
            />
          ))}
        </div>
      </div>
    );
  }
);

AudioSection.displayName = "AudioSection";

export default AudioSection;
