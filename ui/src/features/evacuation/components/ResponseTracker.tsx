import StandaloneExerciseEntry from "@/features/reports/components/StandaloneExerciseEntry";
import type React from "react";
import type { RunResponse, SimulationOutput } from "../generated/contracts";
import { CrewExplanation, policyNames } from "./explanations";
import { isActiveStatus } from "./replay";
import ResponseFindings, { type FindingsReview } from "./ResponseFindings";

/*
 * 3 · Response Tracker's own parts of the Guided workspace. EvacuationWorkspace keeps the map, run, result, policy and
 * replay clock and places each part, so stages 2 and 3 share one map and one result.
 */

/** The replayed policy, read only, where stage 2 offers its policy buttons. */
export function ReplayedPolicy({ policy }: { policy: string }): React.ReactElement {
  return <p className="replay-policy">Replaying {policyNames[policy]} · selected in 2 · Evacuation &amp; Crew Access</p>;
}

/** The replayed policy's crews, with each crew's demo role and illustrative stop use (`replayPolicy`). */
export function ReplayCrews(props: React.ComponentProps<typeof CrewExplanation>): React.ReactElement {
  return <div className="evac-details"><CrewExplanation {...props}/></div>;
}

/** Why the selected run has no recording to replay yet, and where to get one. */
export function NoReplay({ run }: { run: RunResponse | null }): React.ReactElement {
  const gap = !run ? "No completed run is selected. Run a scenario in 2 · Evacuation & Crew Access, then return here."
    : run.status === "completed" ? "Loading the selected run's recording."
    : isActiveStatus(run.status) ? `The selected run is still ${run.status}. Its replay appears here when it completes.`
    : `The selected run ${run.status === "failed" ? "failed" : "was cancelled"}, so there is no recording to replay. Prepare a new scenario in 2 · Evacuation & Crew Access.`;
  return <div className="evac-empty"><h2>No run to replay yet</h2><p>{gap}</p></div>;
}

/** Stage 3's actions beside the replay: review the run's crew findings, or open the separate synthetic exercise. */
export function ResponseTrackerActions({ question, disabled, review, answer, onReview, exerciseOpen, onOpenExercise }: {
  question: string | null; disabled: boolean; review: FindingsReview | null; answer: SimulationOutput | null;
  onReview: () => void; exerciseOpen: boolean; onOpenExercise: () => void;
}): React.ReactElement {
  return <aside className="evac-investigation" aria-label="Response Tracker actions">
    <ResponseFindings question={question} disabled={disabled} review={review} answer={answer} onReview={onReview} />
    <StandaloneExerciseEntry exerciseOpen={exerciseOpen} onOpen={onOpenExercise} />
  </aside>;
}
