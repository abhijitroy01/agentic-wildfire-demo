import { useResponseRegistryContext } from "@/features/reports/ResponseRegistryContext";
import type React from "react";
import { policyNames } from "./explanations";

/**
 * One display-only line naming the scenario selected in 2 · Evacuation & Crew Access: incident, coordinates, run,
 * policy and run status. It shows no observation time (a known fire's last-seen time is client time), and nothing
 * here is sent anywhere; requests and reloads use the owned simulation session.
 */
export default function ScenarioStrip(): React.ReactElement | null {
  const { scenario, getKnownFires } = useResponseRegistryContext();
  if (!scenario) return null;
  const fire = getKnownFires().find(known => known.fireId === scenario.incidentId);
  const place = fire ? [fire.fireName, `${fire.location.lat.toFixed(5)}, ${fire.location.lng.toFixed(5)}`]
    : ["Selected incident"];
  const run = scenario.runId ? [`Run ${scenario.runId.slice(0, 8)}`, policyNames[scenario.policyId] ?? scenario.policyId,
    scenario.runStatus ?? "Status unavailable"] : ["No run yet"];
  return <section className="scenario-strip" aria-label="Selected scenario"><p>{[...place, ...run].join(" · ")}</p></section>;
}
