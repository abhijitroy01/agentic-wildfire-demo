import type React from "react";
import type { SimulationOutput } from "../generated/contracts";
import { policyNames } from "./explanations";
import { FEET_PER_METER,MILES_PER_KM } from "./units";

/** Format an evidence fact value in US units, or say it is unavailable. */
const factValue = (value: unknown, unit: string | null | undefined): string => {
  if (value == null) return "Unavailable";
  if (typeof value !== "number") return typeof value === "object" ? "Recorded structured evidence" : String(value);
  const conversion: Record<string, [number, string]> = { m: [FEET_PER_METER, "ft"], "m/s": [3.6 * MILES_PER_KM, "mph"],
    s: [1/60, "min"], seconds: [1/60, "min"], vehicle_seconds: [1/60, "vehicle-minutes"] };
  const [scale, label] = conversion[unit || ""] || [1, unit || ""];
  return `${(value*scale).toLocaleString("en-US", {maximumFractionDigits: 2})} ${label}`;
};

/** One Simulation agent answer as plain text: the question, the answer, and its cited facts and comparisons. */
export default function SimulationAnswer({ message }: { message: SimulationOutput }): React.ReactElement {
  return <article><b>{message.question}</b><p>{message.answer}</p>
    {message.evidence?.map(e => <details key={e.evidence_id}><summary>Evidence: {e.view} · {e.evidence_id.slice(0, 8)}</summary>
      <ul>{e.facts.map(f => <li key={f.fact_id}>{f.policy_id ? `${policyNames[f.policy_id]} · ` : ""}{f.metric.replace(/_/g, " ")}: {f.availability === "available" ?
        factValue(f.value, f.unit) : f.reason_code}</li>)}</ul>
      {!!e.comparisons.length && <table><thead><tr><th>Measure</th><th>Difference</th><th>Comparison</th></tr></thead><tbody>
        {e.comparisons.map(c => <tr key={c.comparison_id}><td>{c.metric.replace(/_/g, " ")}</td>
          <td>{c.availability === "available" ? factValue(c.delta, c.unit) : c.reason_code}</td>
          <td>{c.comparison_kind.replace(/_/g, " ")}</td></tr>)}</tbody></table>}
      {e.pagination.has_more && <p>More evidence pages are available.</p>}</details>)}
  </article>;
}
