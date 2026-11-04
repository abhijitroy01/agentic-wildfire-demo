import { TrafficLab } from "@/features/traffic-lab";
import { rememberSimulationMode,restoredSimulationMode,type SimulationMode } from "@/shared/services/viewPersistence";
import { useCallback,useEffect,useState } from "react";
import GuidedWorkspace, { type SearchAreaRequest,type WorkflowStage } from "./EvacuationWorkspace";
import "./workspace.css";
import WorkspaceHeading from "@/shared/components/WorkspaceHeading";

/**
 * Compose guided incident work and free-form research without discarding either workflow's state. In
 * 3 · Response Tracker the same guided workspace replays the run and policy selected in stage 2.
 */
export default function SimulationWorkspace({ visible = true, stage = "evacuation", exerciseOpen = false,
  onOpenExercise = () => undefined, searchAreaRequest = null, onSearchAreaHandled = () => undefined }: {
  visible?: boolean; stage?: WorkflowStage; exerciseOpen?: boolean; onOpenExercise?: () => void;
  searchAreaRequest?: SearchAreaRequest | null; onSearchAreaHandled?: () => void;
}): React.JSX.Element {
  const [mode, setMode] = useState<SimulationMode>(() => restoredSimulationMode(sessionStorage, location.search));
  const [researchOpened, setResearchOpened] = useState(mode === "research");
  const select = useCallback((next: SimulationMode): void => {
    setMode(next); rememberSimulationMode(sessionStorage, next);
    if (next === "research") setResearchOpened(true);
    const url = new URL(location.href);
    url.searchParams.set("simulation_mode", next);
    window.history.replaceState(window.history.state, "", url);
  }, []);
  // A search area from 1 · Investigate is simulated in the guided workflow.
  useEffect(() => { if (searchAreaRequest) select("guided"); }, [searchAreaRequest, select]);
  const responseStage = stage === "response";
  return <div className="simulation-workspace">
    <div className="evac-workspace simulation-heading">
      {responseStage ? <WorkspaceHeading eyebrow="SELECTED RUN REPLAY" title="Response Tracker"
        description="Replay the run and policy selected in 2 · Evacuation & Crew Access, with each crew's demo role." />
        : <><WorkspaceHeading eyebrow="INCIDENT WORKSPACE" title="Evacuation & Crew Access"
          description="Investigate conditions, build a response plan and compare residents leaving with responders arriving." />
        <div className="evac-modes" role="group" aria-label="Simulation workflow">
          <button aria-pressed={mode === "guided"} onClick={() => select("guided")}>Guided scenario</button>
          <button aria-pressed={mode === "research"} onClick={() => select("research")}>Research scenarios</button>
        </div>
        <details className="workflow-note"><summary>About this workflow</summary><p>{mode === "guided"
          ? "Start from a verified incident or reference place. Review the prepared roads, ask the agent and compare controlled variants."
          : "Choose U.S. coordinates, road corridors, demand, crews, traffic and hazard assumptions. Inspect and export the resulting experiments."}
          {" "}Runs continue when you switch workflows; starting another scenario does not cancel them.</p></details></>}
    </div>
    {/* One guided workspace at one position, so stages 2 and 3 share its map, run, policy and replay clock. */}
    <div hidden={!responseStage && mode !== "guided"}><GuidedWorkspace visible={visible && (responseStage || mode === "guided")}
      stage={stage} exerciseOpen={exerciseOpen} onOpenExercise={onOpenExercise}
      searchAreaRequest={searchAreaRequest} onSearchAreaHandled={onSearchAreaHandled} /></div>
    {researchOpened && <div hidden={responseStage || mode !== "research"}>
      <TrafficLab visible={visible && !responseStage && mode === "research"} /></div>}
  </div>;
}
