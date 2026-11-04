import type React from "react";
import { createContext,useCallback,useRef,useState } from "react";
import { PANEL_DIMENSIONS,PANEL_Z_INDEX } from "./constants";
import type { PanelManagerContextValue,PanelRegistration } from "./types";

const PanelManagerContext = createContext<PanelManagerContextValue | null>(null);

export { PanelManagerContext };

export const PanelProvider: React.FC<{ children: React.ReactNode }> = ({
  children,
}) => {
  const [openPanels, setOpenPanels] = useState<string[]>([]);
  const registryRef = useRef<Map<string, PanelRegistration>>(new Map());

  const openPanel = useCallback((id: string) => {
    setOpenPanels((prev) => {
      if (prev.includes(id)) return prev;
      return [...prev, id];
    });
  }, []);

  const closePanel = useCallback((id: string) => {
    setOpenPanels((prev) => prev.filter((panelId) => panelId !== id));
  }, []);

  const isPanelOpen = useCallback(
    (id: string) => openPanels.includes(id),
    [openPanels]
  );

  const getZIndex = useCallback(
    (id: string): number => {
      const index = openPanels.indexOf(id);
      if (index === -1) return PANEL_Z_INDEX.DOCKED_BASE;
      const registration = registryRef.current.get(id);
      if (registration?.type === "modal") return PANEL_Z_INDEX.MODAL_CONTENT;
      return PANEL_Z_INDEX.DOCKED_BASE + index * PANEL_Z_INDEX.DOCKED_INCREMENT;
    },
    [openPanels]
  );

  const getEdgeOffset = useCallback(
    (id: string): number => {
      const registration = registryRef.current.get(id);
      if (!registration || registration.type === "modal") return 0;

      const sameEdgePanels = openPanels.filter((openId) => {
        const reg = registryRef.current.get(openId);
        return (
          reg &&
          reg.type === "docked" &&
          reg.position === registration.position
        );
      });

      const myIndex = sameEdgePanels.indexOf(id);
      if (myIndex <= 0) return PANEL_DIMENSIONS.EDGE_GAP;

      let offset = PANEL_DIMENSIONS.EDGE_GAP;
      for (let i = 0; i < myIndex; i++) {
        const prevReg = registryRef.current.get(sameEdgePanels[i]);
        offset +=
          (prevReg?.width || PANEL_DIMENSIONS.DEFAULT_WIDTH) +
          PANEL_DIMENSIONS.STACK_GAP;
      }
      return offset;
    },
    [openPanels]
  );

  const registerPanel = useCallback((registration: PanelRegistration) => {
    registryRef.current.set(registration.id, registration);
  }, []);

  const unregisterPanel = useCallback((id: string) => {
    registryRef.current.delete(id);
    setOpenPanels((prev) => prev.filter((panelId) => panelId !== id));
  }, []);

  const getOpenPanels = useCallback(() => openPanels, [openPanels]);

  const value: PanelManagerContextValue = {
    openPanel,
    closePanel,
    isPanelOpen,
    getZIndex,
    getEdgeOffset,
    registerPanel,
    unregisterPanel,
    getOpenPanels,
  };

  return (
    <PanelManagerContext.Provider value={value}>
      {children}
    </PanelManagerContext.Provider>
  );
};