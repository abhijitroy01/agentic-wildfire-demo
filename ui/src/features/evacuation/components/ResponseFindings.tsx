import AgentActivity from "@/shared/components/AgentActivity";
import type { ToolEvent } from "@/shared/services/toolActivity";
import type React from "react";
import type { SimulationOutput } from "../generated/contracts";
import { policyNames } from "./explanations";
import SimulationAnswer from "./SimulationAnswer";

/**
 * A live review: the policy it was asked under, the Simulation agent's tool activity from the broker's notices, and
 * how the turn ended.
 */
export interface FindingsReview {
  readonly policyId: string;
  readonly status: "running" | "completed" | "failed";
  readonly events: ToolEvent[];
}

/** The review goes straight to the Simulation agent, not through the investigation supervisor. */
export const DIRECT_ACTIVITY_TITLE = "Agent activity · sent directly to the Simulation agent";

/** The review's status line: the turn in progress for the policy it was asked under, a failure, or nothing. */
const reviewStatus = (review: FindingsReview | null): string =>
  review?.status === "running"
    ? `The Simulation agent is reviewing the crew evidence for ${policyNames[review.policyId] ?? review.policyId}.`
    : review?.status === "failed" ? "The review did not complete. Saved evidence remains available; try again." : "";

/**
 * "Review response findings" in 3 · Response Tracker: one question to the Simulation agent about the crew evidence
 * saved with the selected run, for the selected policy. `question` is the run's crew_details suggestion, or null when
 * that evidence is unavailable. Illustrative numbers are never part of the question.
 */
export default function ResponseFindings({ question, disabled, review, answer, onReview }: {
  question: string | null; disabled: boolean; review: FindingsReview | null;
  answer: SimulationOutput | null; onReview: () => void;
}): React.ReactElement {
  return <section className="response-findings" aria-label="Response findings">
    <h3>Response findings</h3>
    <p>The Simulation agent reviews the crew evidence saved with this run for the selected policy. Illustrative
      numbers are not sent.</p>
    <button type="button" disabled={disabled || !question || review?.status === "running"} onClick={onReview}>
      Review response findings</button>
    {!question && <p>Crew evidence is not available for this run.</p>}
    {/* Always mounted, so assistive technology announces each change of its text. */}
    <p role="status">{reviewStatus(review)}</p>
    {review && <AgentActivity events={review.events} title={DIRECT_ACTIVITY_TITLE} />}
    {answer && <SimulationAnswer message={answer} />}
  </section>;
}
