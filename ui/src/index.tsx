import React from "react";
import ReactDOM from "react-dom/client";
import App from "./features/layout/components/App";
import { ResponseRegistryProvider } from "./features/reports/ResponseRegistryContext";
import "./index.css";
import { PanelProvider } from "./shared/components/panels";

const root = ReactDOM.createRoot(document.getElementById("root")!);
root.render(
  <React.StrictMode>
    <ResponseRegistryProvider>
      <PanelProvider>
        <App />
      </PanelProvider>
    </ResponseRegistryProvider>
  </React.StrictMode>
);
