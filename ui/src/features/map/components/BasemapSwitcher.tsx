/**
 * BasemapSwitcher - Names the maps' basemap: every map uses OpenStreetMap
 */
import type React from "react";
import { memo } from "react";
import "./BasemapSwitcher.css";

const BasemapSwitcher: React.FC = memo(() => (
  <div className="basemap-switcher public-basemap-note" role="note">
    <span className="basemap-switcher-button">OpenStreetMap</span>
  </div>
));

BasemapSwitcher.displayName = "BasemapSwitcher";

export default BasemapSwitcher;
