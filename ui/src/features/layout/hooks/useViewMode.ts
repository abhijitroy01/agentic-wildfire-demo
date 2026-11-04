import { SIMULATION_ENABLED } from "@/shared/config";
import { rememberView,restoredView,type ViewMode } from "@/shared/services/viewPersistence";
import { tabStorage } from "@/shared/services/tabStorage";
import { useCallback,useState } from "react";

export type { ViewMode } from "@/shared/services/viewPersistence";

interface UseViewModeReturn {
  viewMode: ViewMode;
  /** Show a view and remember it for reloads (only stages 2 and 3 are remembered); closes the standalone exercise. */
  selectView: (view: ViewMode) => void;
  /** The standalone firetruck exercise: a sub-view of 3 · Response Tracker that a reload does not reopen. */
  exerciseOpen: boolean;
  openExercise: () => void;
  closeExercise: () => void;
}

/**
 * useViewMode - Manages the top-level view: the numbered incident workflow stages.
 *
 * Opening 2 · Evacuation & Crew Access or 3 · Response Tracker remembers it for the browser tab, so a reload
 * reopens it. Returning to Investigation forgets it, so the next reload starts in Investigation.
 */
export function useViewMode(): UseViewModeReturn {
  const [viewMode, setViewMode] = useState<ViewMode>(() => restoredView(tabStorage, SIMULATION_ENABLED, location.search));
  const [exerciseOpen, setExerciseOpen] = useState(false);

  const selectView = useCallback((view: ViewMode): void => {
    setViewMode(view);
    setExerciseOpen(false);
    rememberView(tabStorage, view);
  }, []);
  const openExercise = useCallback((): void => setExerciseOpen(true), []);
  const closeExercise = useCallback((): void => setExerciseOpen(false), []);
  return { viewMode, selectView, exerciseOpen, openExercise, closeExercise };
}
