import type React from "react";
import { memo } from "react";
import { PANEL_Z_INDEX } from "../constants";
import type { FloatingActionButtonProps } from "../types";
import "./FloatingActionButton.css";

const FloatingActionButton: React.FC<FloatingActionButtonProps> = memo(
  ({
    onClick,
    icon,
    badge,
    position = "bottom-right",
    "aria-label": ariaLabel,
    className,
    pulseBadge = false,
  }) => (
    <button
      className={`fab fab--${position} ${className || ""}`}
      onClick={onClick}
      aria-label={ariaLabel}
      style={{ zIndex: PANEL_Z_INDEX.FAB }}
    >
      <span className="fab__icon" aria-hidden="true">
        {icon}
      </span>
      {badge !== undefined && badge !== 0 && badge !== "" && (
        <span className={`fab__badge ${pulseBadge ? "fab__badge--pulse" : ""}`}>
          {badge}
        </span>
      )}
    </button>
  )
);

FloatingActionButton.displayName = "FloatingActionButton";

export default FloatingActionButton;