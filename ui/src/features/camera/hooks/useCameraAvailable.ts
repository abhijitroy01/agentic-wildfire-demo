import { useEffect, useState } from "react";
import { cameraAvailable } from "../services/cameraBroker";

/**
 * Whether map popups may offer "View Camera Feed": false until the workspace reports a configured camera
 * (camera_configured).
 */
export function useCameraAvailable(): boolean {
  const [available, setAvailable] = useState(false);
  useEffect(() => {
    let active = true;
    void cameraAvailable().then((value) => {
      if (active) setAvailable(value);
    });
    return () => {
      active = false;
    };
  }, []);
  return available;
}

export default useCameraAvailable;
