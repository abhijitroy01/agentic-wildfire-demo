/**
 * Reusable Accordion Component
 * Used for: Forecast days, Citations, Audio messages, etc.
 */
import type React from "react";
import { memo,type ReactNode,useId } from "react";
import "./Accordion.css";

type AccordionVariant = "default" | "forecast" | "citation" | "audio";

interface AccordionProps {
  title: string;
  children: ReactNode;
  isExpanded?: boolean;
  onToggle?: () => void;
  headerContent?: ReactNode;
  variant?: AccordionVariant;
  badge?: string | number | null;
}

const Accordion: React.FC<AccordionProps> = memo(
  ({
    title,
    children,
    isExpanded = false,
    onToggle,
    headerContent = null,
    variant = "default",
    badge = null,
  }) => {
    const contentId = useId();
    return (
      <div
        className={`accordion ${variant} ${
          isExpanded ? "expanded" : "collapsed"
        }`}
      >
        <button type="button" className="accordion-header" aria-expanded={isExpanded} aria-controls={contentId} onClick={onToggle}>
          <span className="accordion-title">
            <span className="accordion-toggle" aria-hidden="true">{isExpanded ? "▼" : "▶"}</span>
            <span className="accordion-title-text">{title}</span>
          </span>
          {badge && <span className="accordion-badge">{badge}</span>}
          {headerContent && (
            <span className="accordion-header-content">{headerContent}</span>
          )}
        </button>
        {isExpanded && <div className="accordion-content" id={contentId}>{children}</div>}
      </div>
    );
  }
);

Accordion.displayName = "Accordion";

export default Accordion;
