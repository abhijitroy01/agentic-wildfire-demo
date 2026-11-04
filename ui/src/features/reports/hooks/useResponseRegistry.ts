/**
 * Response Registry Hook (TypeScript)
 *
 * Holds the state the workflow stages share in this browser tab:
 * - Known fires from investigation answers and stage 2 scenarios
 * - The scenario stage 2 selected, for the selected-scenario strip
 */

import { logger } from "@/shared/utils/logger";
import { useCallback,useState } from "react";
import type { KnownFire } from "../types/fireInventory";

/**
 * The scenario selected in 2 · Evacuation & Crew Access, for display only (the selected-scenario strip). The owned
 * simulation session stays authoritative: this is never sent in a request.
 */
export interface ScenarioSelection {
  incidentId: string;
  runId: string | null;
  policyId: string;
  runStatus: string | null;
}

const sameScenario = (a: ScenarioSelection | null, b: ScenarioSelection | null): boolean =>
  a === b || (!!a && !!b && a.incidentId === b.incidentId && a.runId === b.runId && a.policyId === b.policyId
    && a.runStatus === b.runStatus);

export interface UseResponseRegistryReturn {
  registerFires: (fires: KnownFire[]) => void;
  getKnownFires: () => KnownFire[];
  /** The scenario stage 2 last published, or null. */
  scenario: ScenarioSelection | null;
  /** Publish stage 2's selection; an equal selection changes nothing. */
  publishScenario: (scenario: ScenarioSelection | null) => void;
}

// --- Hook ---

/**
 * Response Registry Hook
 */
export function useResponseRegistry(): UseResponseRegistryReturn {
  const [knownFires, setKnownFires] = useState<Map<string, KnownFire>>(() => new Map());

  const [scenario, setScenario] = useState<ScenarioSelection | null>(null);
  const publishScenario = useCallback((next: ScenarioSelection | null): void => {
    setScenario((previous) => (sameScenario(previous, next) ? previous : next));
  }, []);

  /**
   * Register fires from agent detection queries
   */
  const registerFires = useCallback((fires: KnownFire[]): void => {
    setKnownFires((prev) => {
      const newKnownFires = new Map(prev);

      fires.forEach((fire) => {
        newKnownFires.set(fire.fireId, fire);
      });

      logger.log(`🔥 Registered ${fires.length} fires in inventory`);

      return newKnownFires;
    });
  }, []);

  /**
   * Get all known fires as array
   */
  const getKnownFires = useCallback((): KnownFire[] => {
    return Array.from(knownFires.values());
  }, [knownFires]);

  return {
    registerFires,
    getKnownFires,
    scenario,
    publishScenario,
  };
}

export default useResponseRegistry;
