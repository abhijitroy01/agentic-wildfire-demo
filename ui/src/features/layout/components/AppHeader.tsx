/**
 * AppHeader - Application header with logo, tagline and the numbered incident workflow
 */
import { SIMULATION_ENABLED } from "@/shared/config";
import type React from "react";
import type { ViewMode } from "../hooks/useViewMode";

interface AppHeaderProps {
  viewMode: ViewMode;
  onNavigate: (view: ViewMode) => void;
}

/** The workflow stages in order. Builds without simulations omit stage 2 and keep the numbers. */
const STAGES: ReadonlyArray<{ readonly view: ViewMode; readonly label: string }> = [
  { view: "chat", label: "1 · Investigate" },
  ...(SIMULATION_ENABLED ? [{ view: "evacuation" as const, label: "2 · Evacuation & Crew Access" }] : []),
  { view: "response", label: "3 · Response Tracker" },
];

const AppHeader: React.FC<AppHeaderProps> = ({ viewMode, onNavigate }) => {
  return (
    <header className="app-header">
      <div className="header-left">
        <div className="logo-container">
          <img src="/fireguard-ai.png" alt="FIREGUARD AI" className="app-logo" />
          <h1>FIREGUARD AI</h1>
          <div className="tagline">Agentic wildfire planning demonstration</div>
        </div>
      </div>
      <div className="header-right">
        <div className="header-buttons">
          <nav aria-label="Incident workflow" className="workflow-nav">
            <ol>
              {STAGES.map(({ view, label }) => {
                // The current step is announced and does nothing when chosen again.
                const current = viewMode === view;
                return <li key={view}>
                  <button type="button" aria-current={current ? "step" : undefined}
                    onClick={current ? undefined : () => onNavigate(view)}>{label}</button>
                </li>;
              })}
            </ol>
          </nav>
        </div>
      </div>
    </header>
  );
};

export default AppHeader;
