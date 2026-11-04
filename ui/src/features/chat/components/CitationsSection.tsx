import MetadataDisplay from "@/features/chat/components/MetadataDisplay";
import Accordion from "@/shared/components/Accordion";
import type { KnowledgeBaseCitation } from "@/shared/services/agentResponseParser";
import { allowedHttpsUrl } from "@/shared/utils/urlAllowlist";
import type React from "react";
import { memo } from "react";
import "./CitationsSection.css";

// --- Type Definitions ---

type CitationData = KnowledgeBaseCitation;

/** Official guidance sites a knowledge-base citation may link to. */
const OFFICIAL_SOURCE_HOSTS = ["www.ready.gov", "www.fs.usda.gov", "www.nwcg.gov", "www.nps.gov"];
const officialSourceUrl = (value?: string): string | undefined =>
  allowedHttpsUrl(value, host => OFFICIAL_SOURCE_HOSTS.includes(host));

interface CitationMetadata {
  source?: string;
  [key: string]: string | number | null | undefined;
}

interface CitationsSectionProps {
  citations: CitationData[];
  metadata?: CitationMetadata | null;
  usesAccordion?: boolean;
  isExpanded?: boolean;
  onToggle?: () => void;
  foldArticles?: boolean;
}

interface CitationItemProps {
  data: CitationData;
  index: number;
  compact?: boolean;
  folded?: boolean;
}

/**
 * CitationItem - renders individual citation
 * 
 * STRICT SCHEMA COMPLIANCE: Expects documentName and excerpt from KnowledgeBaseCitation schema.
 * Fallbacks removed - if fields are missing, the backend should be fixed.
 */
const CitationItem: React.FC<CitationItemProps> = memo(
  ({ data, index, compact, folded }) => {
    const content = <div className={`citation-content ${compact ? "compact" : ""}`}>
        <div className={`citation-document ${compact ? "compact" : ""}`}>
          {officialSourceUrl(data.sourceUrl)
            ? <a href={officialSourceUrl(data.sourceUrl)} target="_blank" rel="noopener noreferrer">{data.documentName}</a>
            : data.documentName}
        </div>
        {data.publisher && <div>{data.publisher}</div>}
        {data.sourceDate && <div>Source date: {data.sourceDate}</div>}
        {data.retrievedAt && <div>Collection reviewed: {data.retrievedAt}</div>}
        <div className={`citation-excerpt ${compact ? "compact" : ""}`}>
          {compact && !folded && data.excerpt && data.excerpt.length > 150
            ? `${data.excerpt.substring(0, 150)}...`
            : data.excerpt
              ? `"${data.excerpt}"`
              : null}
        </div>
      </div>;
    return folded ? <details className="citation-item folded">
      <summary>{index + 1}. {data.documentName}{data.publisher && <span> · {data.publisher}</span>}</summary>
      {content}
    </details> : <div className={`citation-item ${compact ? "compact" : ""}`}>
      <div className={`citation-number ${compact ? "compact" : ""}`}>{index + 1}</div>{content}
    </div>;
  }
);

CitationItem.displayName = "CitationItem";

const CitationsSection: React.FC<CitationsSectionProps> = memo(
  ({ citations, metadata, usesAccordion, isExpanded, onToggle, foldArticles = true }) => {
    if (usesAccordion) {
      return (
        <Accordion
          title={`📚 Sources (${citations.length})`}
          isExpanded={isExpanded}
          onToggle={onToggle}
          variant="citation"
        >
          {metadata && (
            <MetadataDisplay metadata={metadata} variant="compact" />
          )}
          <div className="citation-list-compact">
            {citations.map((c, i) => (
              <CitationItem key={i} data={c} index={i} compact folded={foldArticles} />
            ))}
          </div>
        </Accordion>
      );
    }

    return (
      <div className="detail-section-full">
        {metadata && <MetadataDisplay metadata={metadata} variant="default" />}
        {citations.map((c, i) => (
              <CitationItem key={i} data={c} index={i} folded={foldArticles} />
        ))}
      </div>
    );
  }
);

CitationsSection.displayName = "CitationsSection";

export default CitationsSection;
