import { useContext } from "react";
import { PanelManagerContext } from "./PanelProvider";
import type { PanelManagerContextValue } from "./types";

export function usePanelManager(): PanelManagerContextValue {
  const context = useContext(PanelManagerContext);
  if (!context) {
    throw new Error(
      "usePanelManager must be used within a <PanelProvider>. " +
        "Wrap your app tree in <PanelProvider> in index.tsx."
    );
  }
  return context;
}