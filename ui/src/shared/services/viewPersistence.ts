// Top-level view persistence for one browser tab. Dependency-free so Node tests can load it.

/**
 * The incident workflow's stages: "chat" is 1 · Investigate, "evacuation" is 2 · Evacuation & Crew Access (one
 * workspace for both simulation workflows) and "response" is 3 · Response Tracker.
 */
export type ViewMode = "chat" | "evacuation" | "response";
export type SimulationMode = "guided" | "research";

export const VIEW_KEY = "fireguard_view";
const MODE_KEY = "fireguard_simulation_mode";
type ViewStorage = Pick<Storage, "getItem" | "setItem" | "removeItem">;
function read(storage: Pick<Storage, "getItem">, key: string): string | null {
  try { return storage.getItem(key); } catch { return null; }
}

/**
 * The view to open on load: a remembered stage 3, a `?traffic_run=` link or a remembered stage 2 when this build
 * includes simulations, else Investigation. A tab that last showed stage 3 reopens it even when a research run it
 * opened earlier left `?traffic_run=` in the address; a fresh tab with the link opens stage 2.
 */
export function restoredView(storage: Pick<Storage, "getItem">, simulationEnabled: boolean, search = ""): ViewMode {
  const saved = read(storage, VIEW_KEY);
  if (saved === "response") return "response";
  if (simulationEnabled && new URLSearchParams(search).get("traffic_run")) return "evacuation";
  return simulationEnabled && (saved === "evacuation" || saved === "traffic-lab") ? "evacuation" : "chat";
}

/** Preserve old Traffic Lab bookmarks and tabs when restoring the combined workspace. */
export function restoredSimulationMode(storage: Pick<Storage, "getItem">, search = ""): SimulationMode {
  const params = new URLSearchParams(search), explicit = params.get("simulation_mode");
  if (explicit === "guided" || explicit === "research") return explicit;
  return params.get("traffic_run") || read(storage, VIEW_KEY) === "traffic-lab"
    || read(storage, MODE_KEY) === "research" ? "research" : "guided";
}

export function rememberSimulationMode(storage: ViewStorage, mode: SimulationMode): void {
  try { storage.setItem(MODE_KEY, mode); } catch { /* Current UI state still owns the selected mode. */ }
}

/** Remember stage 2 or 3 for reloads; switching to Investigation forgets it. */
export function rememberView(storage: ViewStorage, view: ViewMode): void {
  try {
    if (view === "evacuation" || view === "response") storage.setItem(VIEW_KEY, view);
    else storage.removeItem(VIEW_KEY);
  } catch { /* Current UI state still owns the selected view. */ }
}
