import { useCallback,useEffect,useState } from "react";

interface UseCameraModalReturn {
  isCameraModalOpen: boolean;
  cameraFireName: string | undefined;
  closeCameraModal: () => void;
}

/**
 * useCameraModal - Manages camera feed modal state and the window event
 * listener that opens it from map popups.
 *
 * The `openCameraFeed` CustomEvent is dispatched by map popup HTML
 * (generated in popupGenerators.ts) when the user clicks a camera link.
 * This hook listens for that event and opens the modal with the correct fire name.
 */
export function useCameraModal(): UseCameraModalReturn {
  const [isCameraModalOpen, setIsCameraModalOpen] = useState(false);
  const [cameraFireName, setCameraFireName] = useState<string | undefined>(undefined);

  // Listen for camera feed open events from map popups
  useEffect(() => {
    const handleOpenCameraFeed = (event: CustomEvent<{ fireName?: string }>) : void => {
      setCameraFireName(event.detail?.fireName);
      setIsCameraModalOpen(true);
    };

    window.addEventListener("openCameraFeed", handleOpenCameraFeed as EventListener);
    return () => {
      window.removeEventListener("openCameraFeed", handleOpenCameraFeed as EventListener);
    };
  }, []);

  const closeCameraModal = useCallback(() => {
    setIsCameraModalOpen(false);
  }, []);

  return {
    isCameraModalOpen,
    cameraFireName,
    closeCameraModal,
  };
}
