import { TIMING } from "@/shared/constants";
import { logger } from "@/shared/utils/logger";
import type Graphic from "@arcgis/core/Graphic";
import Collection from "@arcgis/core/core/Collection";
import * as reactiveUtils from "@arcgis/core/core/reactiveUtils";
import ActionButton from "@arcgis/core/support/actions/ActionButton";
import type MapView from "@arcgis/core/views/MapView";
import { useCallback,useEffect } from "react";
import {
CAMERA_FEED_ACTION,
cameraPopupActions,
createCenterPopup,
createDemographicPopup,
createFirePopup,
createVegetationPopup,
escapeHtml,
} from "../utils/popupGenerators";
import { createFireExposurePopup } from "../utils/fireExposureGraphics";

interface UseMapPopupsOptions {
  mapView: MapView | null;
  onFeatureClick?: (graphic: Graphic) => void;
  /** Fire popups offer the camera action only while a camera is available. */
  cameraAvailable: boolean;
}

export function useMapPopups({
  mapView,
  onFeatureClick,
  cameraAvailable,
}: UseMapPopupsOptions): {
  handleMapClick: (event: __esri.ViewClickEvent) => Promise<void>;
} {
  // Set up ArcGIS popup trigger-action event handler
  // This is the proper ArcGIS pattern for handling custom action buttons in popups
  useEffect(() => {
    if (!mapView?.popup) return;

    // Use reactiveUtils.on() with accessor function per official ArcGIS documentation
    // Reference: https://developers.arcgis.com/javascript/latest/sample-code/popup-actions/
    const handle = reactiveUtils.on(
      () => mapView.popup,
      "trigger-action",
      (event: __esri.PopupTriggerActionEvent) => {
        // Check if the camera feed action was clicked
        if (event.action.id === CAMERA_FEED_ACTION.id) {
          // Get fire name from the currently selected feature's attributes
          // Support multiple attribute name patterns:
          // - fire.name: From our custom fire graphics
          // - IncidentName: From NIFC wildfire perimeter FeatureLayer
          // - name: Generic fallback
          const selectedFeature = mapView.popup?.selectedFeature;
          const fireName =
            selectedFeature?.attributes?.fire?.name ||
            selectedFeature?.attributes?.IncidentName ||
            selectedFeature?.attributes?.name ||
            "Unknown Fire";

          // Dispatch custom event to open camera feed modal
          window.dispatchEvent(
            new CustomEvent("openCameraFeed", {
              detail: { fireName },
            }),
          );
        }
      },
    );

    return () => {
      handle.remove();
    };
  }, [mapView]);

  const handleMapClick = useCallback(
    async (event: __esri.ViewClickEvent) => {
      if (!mapView || !mapView.map || !mapView.popup) return;

      const viewPopup = mapView.popup;

      try {
        const response = await mapView.hitTest(event, {
          include: mapView.map.allLayers,
        });

        if (response.results.length > 0) {
          const hitResult = response.results[0];
          if ("graphic" in hitResult) {
            const graphic = hitResult.graphic;

            if (graphic.popupTemplate) {
              // Defensive check - viewPopup.open may not be available in all contexts
              if (typeof viewPopup?.open === "function") {
                viewPopup.open({
                  features: [graphic],
                  location: event.mapPoint,
                });
              }

              // mapGraphicsLoader.ts puts any camera action in popupTemplate.actions; ArcGIS renders it.

              onFeatureClick?.(graphic);
              return;
            }

            const attributes = graphic.attributes || {};
            let popupData: { title: string; content: string };
            let includesCameraAction = false;

            const fire = attributes.fire;
            const weather = attributes.currentWeather;

            if (fire) {
              popupData = createFirePopup(fire, weather);
              includesCameraAction = true; // Fire popups get the camera action
            } else {
              switch (attributes.type) {
                case "demographic":
                  popupData = createDemographicPopup(attributes);
                  break;
                case "federalLand":
                  popupData = createVegetationPopup(attributes);
                  break;
                case "fireExposure":
                  popupData = createFireExposurePopup(attributes);
                  break;
                case "center":
                case "weather":
                  popupData = createCenterPopup(attributes);
                  break;
                default:
                  popupData = {
                    title: "Map Feature",
                    content: `<div>Type: ${escapeHtml(attributes.type) || "Unknown"}</div>`,
                  };
              }
            }

            // Defensive check - viewPopup methods may not be available in all contexts
            if (typeof viewPopup?.close === "function") {
              viewPopup.close();
            }

            setTimeout(() => {
              // Open the popup first - with defensive check
              if (typeof viewPopup?.open !== "function") return;
              viewPopup.open({
                title: popupData.title,
                content: popupData.content,
                location: event.mapPoint,
              });

              // Set actions AFTER the popup is opened (ArcGIS requires this order)
              viewPopup.actions = new Collection<ActionButton>(
                cameraPopupActions(includesCameraAction && cameraAvailable)
                  .map((action) => new ActionButton(action)),
              );
            }, TIMING.POPUP_DELAY_MS);

            onFeatureClick?.(graphic);
          }
        }
      } catch (error) {
        logger.error("Hit test error:", error);
      }
    },
    [mapView, onFeatureClick, cameraAvailable],
  );

  return { handleMapClick };
}

export default useMapPopups;
