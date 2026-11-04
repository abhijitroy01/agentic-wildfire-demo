import { corridorLabel,gateCoordinates } from "../artifacts";
import { useLiveMonitoring } from "@/features/radio-monitor/hooks/useLiveMonitoring";
import { useResponseRegistryContext } from "@/features/reports/ResponseRegistryContext";
import type React from "react";
import { useEffect,useMemo,useRef,useState } from "react";
import { useMapFullscreen } from "@/shared/hooks/useMapFullscreen";
import { incidentObservations,observationTime } from "@/shared/domain/incidentObservations";
import type { AgUiEvent } from "@/shared/services/AgentEventStream";
import { closeOpenEvents, receiveSimulationActivity } from "@/shared/services/toolActivity";
import type { FocusRequest,Frame,Network,Result } from "../artifacts";
import type {
CapabilityRegistry,DraftResponse,IncidentResponse,RunResponse,SessionResponse,
SimulationOutput,TurnRequest
} from "../generated/contracts";
import { api,initialize,REPORTED_ADDRESS_MIN_LENGTH,turn } from "../services/api";
import type { ScenarioLocationInput } from "../services/locationInput";
import { BrokerError } from "@/shared/services/workspaceAccess";
import { tabStorage } from "@/shared/services/tabStorage";
import { newerStatus, readDelay, temporaryRead } from "../services/readRecovery";
import {
  acceptWorkflowLocation, clearWorkflowStart, createWorkflowStart, prepareWorkflowRoads, readFireCatalog,
  readRetainedWorkflows, readWorkflowStart, reportedFireIdentity, retainFireCatalog, roadPreparationIntent, saveWorkflowStart,
  workflowObservations,
} from "../services/workflowStart";
import type { RetainedWorkflow, WorkflowStartIntent } from "../services/workflowStart";
import { Comparison } from "./Comparison";
import { MissionComparison, MissionFindings } from "./MissionComparison";
import { PolicyInsights } from "./PolicyInsights";
import { REPLAY_MAP_ID,SimulationMap } from "./SimulationMap";
import type { FindingsReview } from "./ResponseFindings";
import { NoReplay,ReplayCrews,ReplayedPolicy,ResponseTrackerActions } from "./ResponseTracker";
import SimulationAnswer from "./SimulationAnswer";
import { ProviderReadings,SourceDetails } from "./SourceEvidence";
import { UnitInput } from "./UnitInput";
import { BottleneckExplanation,CrewExplanation,MapGuide,MetricCards,policyNames,RadioStatus,ReplayExplanation,ReplayStatus,RunEvidence } from "./explanations";
import { crewsReturned } from "./crews";
import "./lab-presentation.css";
import { crewRecords } from "./mission";
import { clock,isActiveStatus,peakSample } from "./replay";
import { TrafficEvidence } from "./traffic";
import { useIncidentInvestigation } from "../hooks/useIncidentInvestigation";
import IncidentInvestigation, { IncidentConditions, investigationActivity } from "./IncidentInvestigation";
import CoordinateEntry from "./CoordinateEntry";
import { miles,MILES_PER_KM } from "./units";
import "./workspace.css";

const active = (run: RunResponse | null): boolean => !!run && isActiveStatus(run.status);
const key = "fireguard_simulation_session";
/** A completed answer that cites saved evidence from `runId`. */
const citesRun = (message: SimulationOutput, runId: string): boolean => message.status === "answer" &&
  !!message.evidence?.some(evidence => evidence.artifact_refs.some(ref => ref.run_id === runId));
const hasSavedRunEvidence = (messages: SimulationOutput[], runId: string): boolean =>
  messages.some(message => citesRun(message, runId));
/**
 * Whether an answer has policy-bearing facts and every one names `policyId`. A saved turn records no policy of its own,
 * so an answer citing several policies (an evidence call without policy_ids) fails closed and stands in under none.
 */
const citesOnlyPolicy = (message: SimulationOutput, policyId: string): boolean => {
  const cited = (message.evidence ?? []).flatMap(evidence =>
    evidence.facts.flatMap(fact => fact.policy_id ? [fact.policy_id] : []));
  return cited.length > 0 && cited.every(cite => cite === policyId);
};
/** The live "Review response findings" turn for one run, under the policy it was asked for. */
interface Review extends FindingsReview { readonly runId: string; readonly turnId?: string }
/** Whether `review` was asked about this run under this policy. */
const isReviewOf = (review: Review | null, runId: string | undefined, policyId: string): review is Review =>
  !!review && review.runId === runId && review.policyId === policyId;

/** The numbered stage this workspace serves: 2 · Evacuation & Crew Access, or the 3 · Response Tracker replay. */
export type WorkflowStage = "evacuation" | "response";

/** An explicit investigation handoff: either its search place or saved reported fire, started once by `id`. */
export type SearchAreaRequest = { readonly id: string; readonly kind: "search-area"; readonly place: string }
  | { readonly id: string; readonly kind: "reported-fire"; readonly assessmentId: string };

/**
 * Evacuation workspace page: session, incident location, drafts, runs and agent turns. In stage 3 the same instance
 * replays the selected run, policy and clock with crew demo roles, placing the parts from ResponseTracker, so stages 2
 * and 3 cannot disagree.
 */
export default function EvacuationWorkspace({ visible = true, stage = "evacuation", exerciseOpen = false,
  onOpenExercise = () => undefined, searchAreaRequest = null, onSearchAreaHandled = () => undefined }: {
  visible?: boolean; stage?: WorkflowStage; exerciseOpen?: boolean; onOpenExercise?: () => void;
  searchAreaRequest?: SearchAreaRequest | null; onSearchAreaHandled?: () => void;
}): React.ReactElement {
  const { registerFires, publishScenario } = useResponseRegistryContext();
  const responseStage = stage === "response";
  const [ready, setReady] = useState(false), [restored, setRestored] = useState(false);
  const [accessFailed, setAccessFailed] = useState(false), [accessAttempt, setAccessAttempt] = useState(0);
  const [readAttempt, setReadAttempt] = useState(0);
  const [artifactFailedId, setArtifactFailedId] = useState<string | null>(null);
  const [networkFailedId, setNetworkFailedId] = useState<string | null>(null);
  const [statusFailedId, setStatusFailedId] = useState<string | null>(null);
  const [mode, setMode] = useState<"radio" | "lab">("lab");
  const [session, setSession] = useState<SessionResponse | null>(null);
  const [incident, setIncident] = useState<IncidentResponse | null>(null);
  const [draft, setDraft] = useState<DraftResponse | null>(null);
  const [run, setRun] = useState<RunResponse | null>(null);
  const [result, setResult] = useState<Result | null>(null), [network, setNetwork] = useState<Network | null>(null);
  const [caps, setCaps] = useState<CapabilityRegistry | null>(null);
  const [messages, setMessages] = useState<SimulationOutput[]>([]);
  const [activity, setActivity] = useState<string[]>([]);
  const [busy, setBusy] = useState(false), [error, setError] = useState("");
  const [question, setQuestion] = useState(""), [address, setAddress] = useState("");
  // The place from 1 · Investigate behind the current scenario; display only, so a reload shows the verified point.
  const [searchArea, setSearchArea] = useState<string | null>(null);
  const [preset, setPreset] = useState("paradise-2018");
  const [policy, setPolicy] = useState("baseline"), [seconds, setSeconds] = useState(0);
  const [panel, setPanel] = useState<"overview" | "bottlenecks" | "crews" | "evidence" | "plans" | "activity">("overview");
  const [contextPanel, setContextPanel] = useState<"scenario" | "conditions" | "assistant">("scenario");
  const [selectedObservation, setSelectedObservation] = useState<string | null>(null);
  const [focusedCrew, setFocusedCrew] = useState<string | null>(null);
  const { expanded, toggle: toggleMap, containerRef: mapContainer } = useMapFullscreen(visible);
  const [playing, setPlaying] = useState(false);
  const [observationFocus, setObservationFocus] = useState(0);
  const [speed, setSpeed] = useState(20);
  useEffect(() => { if (!visible) setPlaying(false); }, [visible]);
  const [showRoutes, setShowRoutes] = useState(true), [showTrails, setShowTrails] = useState(true);
  const [showTraffic, setShowTraffic] = useState(true), [showCongestion, setShowCongestion] = useState(true);
  const [focusRequest, setFocusRequest] = useState<FocusRequest | null>(null);
  const [radius, setRadius] = useState(4);
  const [audio, setAudio] = useState<{ url: string; file: string } | null>(null);
  const radioPlayer = useRef<HTMLAudioElement>(null);
  const [radioPlaying, setRadioPlaying] = useState(false);
  const [transcript, setTranscript] = useState("");
  const [variantVehicles, setVariantVehicles] = useState(600), [variantDeparture, setVariantDeparture] = useState(20);
  const [runs, setRuns] = useState<RunResponse[]>([]);
  const [fireCatalog, setFireCatalog] = useState(() => readFireCatalog(tabStorage));
  const [pendingStart, setPendingStart] = useState(() => readWorkflowStart(tabStorage));
  const [retainedWorkflows, setRetainedWorkflows] = useState(() => readRetainedWorkflows(tabStorage));
  const [conditionsStartError, setConditionsStartError] = useState("");
  const [conditionsWaiting, setConditionsWaiting] = useState(false);
  const [preparationError, setPreparationError] = useState("");
  const [resumePermitted, setResumePermitted] = useState(true);
  const [contextRevision, setContextRevision] = useState(0);
  const [review, setReview] = useState<Review | null>(null);
  const analyzed = useRef<string | null>(null);
  const generation = useRef(0), started = useRef(false), loaded = useRef<string | null>(null);
  const busyOperation = useRef(0);
  const pendingIntent = useRef(pendingStart); pendingIntent.current = pendingStart;
  const catalogSelection = useRef({selectedObservation,fireCatalog}); catalogSelection.current = {selectedObservation,fireCatalog};
  const migratedSources = useRef(new Set<string>());
  const selectedRun = useRef(run); selectedRun.current = run;
  const runId = run?.run_id, runStatus = run?.status;
  const sessionId = session?.session_id, incidentId = incident?.incident_id;
  const investigation = useIncidentInvestigation(incidentId, runId);
  const { start: startInvestigation } = investigation;
  const initialInvestigationStatus = investigation.initial?.status;
  const observations = useMemo(() => workflowObservations(incidentObservations(
    investigation.initial && investigation.initial.incident_id === incidentId ? investigation.initial.snapshot : null), fireCatalog),
    [investigation.initial, incidentId, fireCatalog]);
  useEffect(() => {
    if (investigation.initial?.fire_observations?.length) {
      const next = retainFireCatalog(tabStorage, investigation.initial.fire_observations);
      const selected = catalogSelection.current.fireCatalog.find(value =>
        `fire:${value.observation_id}` === catalogSelection.current.selectedObservation);
      const replacement = selected && next.find(value => reportedFireIdentity(value) === reportedFireIdentity(selected));
      if (replacement) setSelectedObservation(`fire:${replacement.observation_id}`);
      setFireCatalog(next);
    }
  }, [investigation.initial?.fire_observations]);
  useEffect(() => {
    const task = investigation.initial;
    if (!incidentId || task?.incident_id !== incidentId || task.status === "running" ||
      task.fire_observations?.length || migratedSources.current.has(task.task_id) ||
      !incidentObservations(task.snapshot).some(value => !!value.fire)) return;
    migratedSources.current.add(task.task_id);
    const stamp = generation.current;
    void startInvestigation(incidentId).catch(err => {
      if (stamp === generation.current) log(`Saved fire source receipts unavailable: ${err instanceof Error ? err.message : "Reopen this investigation."}`);
    });
  }, [incidentId, investigation.initial, startInvestigation]);
  const selectedObservationId = observations.some(item => item.id === selectedObservation)
    ? selectedObservation : observations[0]?.id || null;
  const planRequested = useRef<string | null>(null);
  const askLatest = useRef(ask); askLatest.current = ask;
  const startLatest = useRef(startScenario); startLatest.current = startScenario;
  const searchAreaHandled = useRef(onSearchAreaHandled); searchAreaHandled.current = onSearchAreaHandled;
  const startedSearchArea = useRef<string | null>(null);
  const currentContext = useRef({ sessionId, runId }); currentContext.current = { sessionId, runId };
  const selectionRef = useRef(""); selectionRef.current = JSON.stringify([run?.run_id, policy, seconds]);
  const log = (message: string): void => setActivity(previous => [...previous.slice(-19), message]);
  useEffect(() => {
    const remaining = (pendingStart?.retryAtMs || 0)-Date.now();
    setResumePermitted(remaining <= 0);
    if (remaining <= 0) return;
    const timer = window.setTimeout(() => setResumePermitted(true), remaining);
    return () => window.clearTimeout(timer);
  }, [pendingStart?.retryAtMs]);

  useEffect(() => {
    if (!incident || incident.location.status !== "resolved") return;
    const name = incident.preset_id === "paradise-2018" ? "Paradise (2018)" :
      incident.preset_id === "palisades-2025" ? "Palisades, Los Angeles (2025)" : incident.location.matched_address;
    registerFires([{fireId: incident.incident_id, fireName: `${name} · simulation`,
      location: {lat: incident.location.lat, lng: incident.location.lon}, lastSeen: new Date().toISOString()}]);
  }, [incident, registerFires]);

  // Display only, for the selected-scenario strip; requests and reloads use the owned session.
  const runOfIncident = !!incidentId && run?.incident_id === incidentId;
  useEffect(() => {
    publishScenario(incidentId ? {incidentId, runId: runOfIncident ? runId ?? null : null, policyId: policy,
      runStatus: runOfIncident ? runStatus ?? null : null} : null);
  }, [incidentId, runOfIncident, runId, runStatus, policy, publishScenario]);
  useEffect(() => () => publishScenario(null), [publishScenario]);

  useEffect(() => {
    let cancelled = false;
    const controller = new AbortController(), { signal } = controller;
    setAccessFailed(false); setReady(false); setRestored(false);
    initialize(signal).then(async () => {
      if (cancelled) return;
      setReady(true);
      setCaps(await api<CapabilityRegistry>("/capabilities", undefined, { signal }));
      setRuns(await api<RunResponse[]>("/runs", undefined, { signal }));
      const saved = tabStorage.getItem(key);
      if (saved) {
        let current: SessionResponse;
        try {
          current = await api<SessionResponse>(`/sessions/${saved}`, undefined, { signal });
        } catch (err) {
          if (!cancelled && err instanceof BrokerError && [404,410].includes(err.httpStatus)) {
            tabStorage.removeItem(key);
            throw new Error("The saved simulation session is no longer available. Reconnect to start a new workflow.");
          }
          throw err;
        }
        if (cancelled) return;
        setSession(current); setMode(current.entry_mode); setPolicy(current.policy_id || "baseline");
        setSeconds(current.replay_time_s || 0);
        let restoredIncident: IncidentResponse | null = null;
        if (current.incident_id) {
          restoredIncident = await api<IncidentResponse>(`/incidents/${current.incident_id}`, undefined, { signal });
          setIncident(restoredIncident);
          if (restoredIncident.preset_id) setPreset(restoredIncident.preset_id);
        }
        let restoredRun: RunResponse | null = null;
        if (current.selected_run_id) {
          const job = await api<RunResponse>(`/runs/${current.selected_run_id}`, undefined, { signal });
          if (!restoredIncident) {
            restoredIncident = await api<IncidentResponse>(`/incidents/${job.incident_id}`, undefined, { signal });
            setIncident(restoredIncident);
            if (restoredIncident.preset_id) setPreset(restoredIncident.preset_id);
          }
          restoredRun = job;
        }
        const history = await api<Array<SimulationOutput & { answer?: string }>>(`/sessions/${saved}/turns`, undefined,
          { signal });
        setMessages(history.filter(m => !!m.answer));
        const latestReceipt = [...history].reverse().find(m => m.draft || m.run);
        const draftId = current.latest_draft_id ||
          (pendingIntent.current?.session?.session_id === saved ? pendingIntent.current.draftId : null);
        if (!restoredRun && draftId) {
          const savedDraft = await api<DraftResponse>(`/scenario-drafts/${draftId}`, undefined, {signal});
          if (!cancelled) setDraft(savedDraft);
        } else if (latestReceipt?.draft && !latestReceipt.run) setDraft(latestReceipt.draft);
        if (restoredRun) {
          // Restore evidence before activating the run. Reanalysis can emit seek/
          // policy actions and must not overwrite a saved selection during reload.
          if (hasSavedRunEvidence(history, restoredRun.run_id)) analyzed.current = restoredRun.run_id;
          setRun(restoredRun);
        }
      }
      if (!cancelled) { setReady(true); setRestored(true); setError(""); }
    }).catch(err => { if (!cancelled) { setAccessFailed(true); setError(String(err.message)); } });
    return () => { cancelled = true; controller.abort(); };
  }, [accessAttempt]);

  // "Simulate this search area": start each request once, after the saved session is restored, so the restore cannot
  // overwrite the new scenario, and after any busy start or turn settles, so the click is not dropped. The place goes
  // through its explicit place or retained-fire path below.
  useEffect(() => {
    if (!ready || !restored || busy || !searchAreaRequest || startedSearchArea.current === searchAreaRequest.id) return;
    startedSearchArea.current = searchAreaRequest.id;
    searchAreaHandled.current();
    // startScenario shows its own failure in the workspace's error banner.
    const place = searchAreaRequest.kind === "search-area";
    startLatest.current("radio", place ? searchAreaRequest.place : searchAreaRequest.assessmentId,
      { from: place ? "search-area" : "reported-fire" }).catch(() => undefined);
  }, [ready, restored, busy, searchAreaRequest]);

  useEffect(() => {
    if (!runId) return;
    const stamp = generation.current;
    let cancelled = false;
    const controller = new AbortController(), { signal } = controller;
    const id = runId;
    let timer: number | undefined, artifactFailures = 0;
    let readError = "";
    const current = (): boolean => !cancelled && stamp === generation.current;
    const later = (delay = 2500): void => { if (current()) timer = window.setTimeout(() => void poll(), delay); };
    const poll = async (): Promise<void> => {
      let downloading = false;
      try {
        const next = await api<RunResponse>(`/runs/${id}`, undefined, { signal, timeoutMs: 15000 });
        if (!current()) return;
        const previous = selectedRun.current;
        const job = previous?.run_id === id ? newerStatus(previous, next) : next;
        selectedRun.current = job;
        setRun(value => value?.run_id === id ? newerStatus(value, job) : value);
        setStatusFailedId(null);
        if (readError) { const recovered = readError; setError(value => value === recovered ? "" : value); readError = ""; }
        if (job.status === "completed" && loaded.current !== id) {
          downloading = true;
          const [data, geometry, capability] = await Promise.all([
            api<Result>(`/runs/${id}/artifacts/result`, undefined, { signal }),
            api<Network>(`/runs/${id}/artifacts/network`, undefined, { signal }),
            api<CapabilityRegistry>(`/runs/${id}/capabilities`, undefined, { signal }),
          ]);
          if (!current()) return;
          downloading = false;
          loaded.current = id; setResult(data); setNetwork(geometry); setCaps(capability);
          setArtifactFailedId(null);
          const retained = await api<RunResponse[]>("/runs", undefined, {signal});
          if (!current()) return;
          setRuns(retained); log("Completed simulation and retained evidence are ready.");
        }
        if (job.status === "failed") setError(job.error || "Simulation failed. No complete comparison is available.");
        if (active(job)) later();
      } catch (err) {
        if (!current()) return;
        readError = err instanceof Error ? err.message : "Could not load run.";
        setError(readError);
        if (downloading) {
          if (temporaryRead(err) && ++artifactFailures < 3) later(readDelay(err, artifactFailures - 1));
          else setArtifactFailedId(id);
        } else if (selectedRun.current?.status === "completed" && loaded.current !== id) {
          if (temporaryRead(err) && ++artifactFailures < 3) later(readDelay(err, artifactFailures - 1));
          else setArtifactFailedId(id);
        } else if (temporaryRead(err) && active(selectedRun.current)) later(readDelay(err));
        else if (active(selectedRun.current)) setStatusFailedId(id);
      }
    };
    void poll();
    return () => { cancelled = true; controller.abort(); window.clearTimeout(timer); };
  }, [runId, contextRevision, readAttempt]);

  useEffect(() => {
    if (!draft?.preparation_id || draft.status !== "preparing") return;
    const stamp = generation.current;
    let cancelled = false;
    const controller = new AbortController(), { signal } = controller;
    let timer: number | undefined, readError = "";
    const later = (delay = 2500): void => {
      if (!cancelled && stamp === generation.current) timer = window.setTimeout(() => void poll(), delay);
    };
    const poll = async (): Promise<void> => {
      try {
        const current = await api<DraftResponse>(`/scenario-drafts/${draft.draft_id}`, undefined,
          { signal, timeoutMs: 15000 });
        if (cancelled || stamp !== generation.current) return;
        setStatusFailedId(null);
        if (readError) { const recovered = readError; setError(value => value === recovered ? "" : value); readError = ""; }
        if (current.status === "ready") {
          log(`Road network validated: ${miles(current.scenario.radius_km)} radius. Review the draft before running.`);
        }
        setDraft(previous => previous?.draft_id === current.draft_id && previous.status === "preparing" ? current : previous);
        if (current.status === "preparing") later();
      } catch (err) {
        if (cancelled || stamp !== generation.current) return;
        readError = err instanceof Error ? err.message : "Could not load preparation.";
        setError(readError);
        if (temporaryRead(err)) later(readDelay(err)); else setStatusFailedId(draft.draft_id);
      }
    };
    void poll();
    return () => { cancelled = true; controller.abort(); window.clearTimeout(timer); };
  }, [draft?.draft_id, draft?.preparation_id, draft?.status, contextRevision, readAttempt]);

  useEffect(() => {
    if (!draft?.preparation_id || draft.status !== "ready" || result) return;
    const stamp = generation.current;
    let cancelled = false;
    const controller = new AbortController();
    let timer: number | undefined, failures = 0, readError = "";
    const load = async (): Promise<void> => {
      try {
        const geometry = await api<Network>(`/scenario-drafts/${draft.draft_id}/network`, undefined,
          {signal: controller.signal});
        if (cancelled || stamp !== generation.current) return;
        setNetwork(geometry); setNetworkFailedId(null);
        if (readError) { const recovered = readError; setError(value => value === recovered ? "" : value); }
      } catch (err) {
        if (cancelled || stamp !== generation.current) return;
        readError = err instanceof Error ? err.message : "Could not load the prepared road network.";
        setError(readError);
        if (temporaryRead(err) && ++failures < 3) timer = window.setTimeout(() => void load(), readDelay(err, failures - 1));
        else setNetworkFailedId(draft.draft_id);
      }
    };
    void load();
    return () => {cancelled = true; controller.abort(); window.clearTimeout(timer);};
  }, [draft?.draft_id, draft?.preparation_id, draft?.status, result, contextRevision, readAttempt]);

  useEffect(() => {
    if (!playing || !result) return;
    const timer = window.setInterval(() => setSeconds(s => {
      const next = Math.min(s + speed / 5, result.scenario.duration_minutes * 60);
      if (next >= result.scenario.duration_minutes * 60) setPlaying(false);
      return next;
    }), 200);
    return () => window.clearInterval(timer);
  }, [playing, result, speed]);

  useEffect(() => {
    if (!sessionId || !runId || busy || pendingStart?.incident) return;
    const stamp = generation.current;
    const timer = window.setTimeout(async () => {
      try {
        if (stamp !== generation.current || currentContext.current.sessionId !== sessionId ||
          currentContext.current.runId !== runId) return;
        const current = await api<SessionResponse>(`/sessions/${sessionId}`);
        if (stamp !== generation.current || currentContext.current.sessionId !== sessionId ||
          currentContext.current.runId !== runId) return;
        const updated = await api<SessionResponse>(`/sessions/${sessionId}`, {
          request_id: crypto.randomUUID(), expected_revision: current.revision,
          selected_run_id: runId, policy_id: policy, replay_time_s: seconds,
        }, { method: "PATCH" });
        if (stamp === generation.current && currentContext.current.sessionId === sessionId &&
          currentContext.current.runId === runId) setSession(updated);
      } catch { /* A conflicting selection never changes the active run; next interaction retries. */ }
    }, 1000);
    return () => window.clearTimeout(timer);
  }, [sessionId, runId, policy, seconds, busy, pendingStart?.incident]);

  useEffect(() => {
    if (!result || !runId || runStatus !== "completed" || busy || !incidentId || !sessionId || analyzed.current === runId) return;
    analyzed.current = runId;
    const stamp = generation.current, operation = ++busyOperation.current;
    setBusy(true);
    askLatest.current("Compare resident evacuation and crew access across all policies.", "analyze")
      .catch(err => {if (stamp === generation.current) setError(err instanceof Error ? err.message : "Analysis unavailable.");})
      .finally(() => {if (operation === busyOperation.current) setBusy(false);});
  }, [result, runId, runStatus, busy, incidentId, sessionId]);

  useEffect(() => {
    if (!incidentId || !runId || runStatus !== "completed" || planRequested.current !== runId ||
      initialInvestigationStatus === "running") return;
    planRequested.current = null;
    const stamp = generation.current;
    startInvestigation(incidentId, runId).catch(err => {
      if (stamp === generation.current) setError(err instanceof Error ? err.message : "Could not start the simulation-informed plan.");
    });
  }, [incidentId, runId, runStatus, initialInvestigationStatus, startInvestigation]);

  async function ask(text: string, intent: TurnRequest["intent"] = "question", { location = incident,
    currentSession = session, selectedDraft = draft, onCustom }: { location?: IncidentResponse | null;
    currentSession?: SessionResponse | null; selectedDraft?: DraftResponse | null; onCustom?: (event: AgUiEvent) => void } = {},
  ): Promise<SimulationOutput> {
    if (!location || !currentSession) throw new Error("Resolve a scenario location first.");
    const stamp = generation.current;
    const selectionAtStart = selectionRef.current;
    const output = await turn({
      request_id: crypto.randomUUID(), session_id: currentSession.session_id,
      incident_id: location.incident_id, location_revision: location.location_revision,
      question: text, intent, run_id: run?.incident_id === location.incident_id ? run.run_id : null, policy_id: policy as TurnRequest["policy_id"],
      replay_time_s: seconds, draft_id: selectedDraft?.draft_id || null,
    }, message => {if (stamp === generation.current) log(message);},
    {onCustom: event => {if (stamp === generation.current) onCustom?.(event);}});
    if (stamp !== generation.current) return output;
    setMessages(previous => [...previous.filter(m => m.turn_id !== output.turn_id), output]);
    if (selectionAtStart === selectionRef.current && output.incident_id === location.incident_id &&
        output.location_revision === location.location_revision) {
      for (const action of output.ui_actions || []) {
        if (action.type === "open_panel" && action.panel) setPanel(action.panel);
        if (action.type === "select_policy" && result?.policies.some((p) => p.policy === action.policy_id)) {
          selectPolicy(action.policy_id!, null);
        }
        if (action.type === "seek_time" && action.time_s != null && result && action.time_s <= result.scenario.duration_minutes*60) {
          setPlaying(false); setSeconds(action.time_s);
        }
        if (action.type === "focus_crew" && result?.crew_plan?.some((c) => c.id === action.crew_id)) setFocusedCrew(action.crew_id!);
      }
    }
    if (output.draft) setDraft(output.draft);
    if (output.run) { planRequested.current = output.run.run_id; setRun(output.run); setDraft(null); }
    if (output.status === "error") throw new Error(output.answer);
    return output;
  }

  function rememberStart(intent: WorkflowStartIntent): void {
    if (pendingIntent.current?.id !== intent.id) return;
    saveWorkflowStart(tabStorage, intent);
    pendingIntent.current = intent; setPendingStart(intent);
    setRetainedWorkflows(readRetainedWorkflows(tabStorage));
  }

  /** Admission is explicit: a busy owner lease waits for a user retry, with no hidden queue. */
  async function beginConditions(location: IncidentResponse, stamp = generation.current): Promise<void> {
    if (stamp === generation.current) {setConditionsStartError(""); setConditionsWaiting(false);}
    try {
      await investigation.start(location.incident_id);
      if (stamp === generation.current) log("Fire, weather and guidance agents started alongside road preparation.");
    } catch (err) {
      if (stamp !== generation.current) return;
      setConditionsWaiting(err instanceof BrokerError && err.code === "INVESTIGATION_IN_PROGRESS");
      setConditionsStartError(err instanceof Error ? err.message : "Conditions could not start. Retry this step.");
    }
  }

  /** Accept one persisted source intent, then prepare its roads directly through the broker. */
  async function continueWorkflow(intent: WorkflowStartIntent, from?: "search-area" | "typed-address" | "reported-fire"):
    Promise<void> {
    if (!ready || !restored || started.current) return;
    started.current = true;
    let stamp = generation.current;
    const operation = ++busyOperation.current;
    pendingIntent.current = intent; rememberStart(intent);
    setBusy(true); setError(""); setPreparationError("");
    try {
      const accepted = await acceptWorkflowLocation(intent, api, rememberStart);
      if (operation !== busyOperation.current) return;
      stamp = ++generation.current; setContextRevision(stamp);
      const created = accepted.session, location = accepted.incident;
      const changedIncident = incident?.incident_id !== location.incident_id;
      if (changedIncident) {
        setSearchArea(from === "search-area" ? intent.label : null);
        if (from) {setTranscript(""); radio.clearDetection();}
        setResult(null); setNetwork(null); setRun(null); setDraft(null);
        setSelectedObservation(location.location.source_observation_id
          ? `fire:${location.location.source_observation_id}` : null);
        setObservationFocus(0); setPanel("overview"); setContextPanel("scenario");
        setMessages([]); setActivity([]); setPlaying(false); setSeconds(0); loaded.current = null;
        setConditionsStartError(""); setConditionsWaiting(false);
      }
      setSession(created); setMode(intent.entryMode); setIncident(location);
      if (location.preset_id) setPreset(location.preset_id);
      else if (!from || from === "typed-address") setAddress(intent.label);
      tabStorage.setItem(key, created.session_id);
      if (changedIncident) {
        log(`${location.location.provider} resolved ${location.location.matched_address} at ${location.location.lat.toFixed(5)}, ${location.location.lon.toFixed(5)}.`);
        void beginConditions(location, stamp);
      }
      const prepared = await prepareWorkflowRoads(accepted, api, rememberStart);
      if (stamp !== generation.current) return;
      setDraft(prepared); setRun(null); setResult(null); setNetwork(null); setPlaying(false);
      clearWorkflowStart(tabStorage, intent.id); pendingIntent.current = null; setPendingStart(null);
      log("Road preparation accepted. Review the validated study area before running.");
    } catch (err) {
      if (stamp === generation.current) {
        const message = err instanceof Error ? err.message : "Could not start this workflow.";
        if (err instanceof BrokerError && [403,404].includes(err.httpStatus)) {
          clearWorkflowStart(tabStorage, intent.id); pendingIntent.current = null; setPendingStart(null);
          setError(`${message} Select a current reported fire or enter a new scenario location.`);
          return;
        }
        if (err instanceof BrokerError && err.retryAfter != null && pendingIntent.current) {
          rememberStart({...pendingIntent.current,retryAtMs:Date.now()+err.retryAfter*1000});
        }
        if (err instanceof BrokerError && err.code === "GEOCODER_OUTCOME_UNKNOWN" && pendingIntent.current) {
          rememberStart({...pendingIntent.current,geocoderOutcomeUnknown:true});
        }
        if (pendingIntent.current?.incident) setPreparationError(message);
        else setError(message);
      }
      throw err;
    } finally {
      started.current = false;
      if (operation === busyOperation.current) setBusy(false);
    }
  }

  /** A new explicit start uses a saved fire receipt, entered U.S. point, address or reference place. */
  async function startScenario(entry: "radio" | "lab", value: string,
    {from, input}: {from?: "search-area" | "typed-address" | "reported-fire"; input?: ScenarioLocationInput} = {}):
    Promise<void> {
    if (started.current) return;
    const source: ScenarioLocationInput = input ?? (entry === "lab"
      ? {preset_id:value as ScenarioLocationInput["preset_id"]} : from === "search-area" ? {reported_place:value}
      : from === "reported-fire" ? {fire_assessment_id:value} : {reported_address:value});
    await continueWorkflow(createWorkflowStart(entry, source, value), from);
  }

  async function prepareAcceptedArea(radiusKm?: number): Promise<void> {
    if (!session || !incident || started.current) return;
    const intent = {...roadPreparationIntent(session, incident), ...(radiusKm === undefined ? {} : {radiusKm})};
    await continueWorkflow(intent);
  }

  /** Rejoin saved work without creating sessions, source observations, jobs or cancellations. */
  async function rejoinWorkflow(saved: RetainedWorkflow, selected?: RunResponse): Promise<void> {
    if (!ready || !restored) return;
    let stamp = generation.current;
    const operation = ++busyOperation.current;
    setBusy(true); setError("");
    try {
      const [current, location, history] = await Promise.all([
        api<SessionResponse>(`/sessions/${saved.sessionId}`), api<IncidentResponse>(`/incidents/${saved.incidentId}`),
        api<SimulationOutput[]>(`/sessions/${saved.sessionId}/turns`),
      ]);
      const savedDraft = current.latest_draft_id
        ? await api<DraftResponse>(`/scenario-drafts/${current.latest_draft_id}`) : null;
      const savedRun = selected || (current.selected_run_id ? await api<RunResponse>(`/runs/${current.selected_run_id}`) : null);
      if (operation !== busyOperation.current) return;
      stamp = ++generation.current; setContextRevision(stamp);
      setSession(current); setIncident(location); setMode(current.entry_mode);
      setPolicy(current.policy_id || "baseline"); setSeconds(current.replay_time_s || 0);
      setDraft(savedRun ? null : savedDraft); setRun(savedRun); setResult(null); setNetwork(null);
      setMessages(history.filter(message => !!message.answer)); setActivity([]); setPlaying(false); setSearchArea(null);
      setPreparationError(""); setConditionsStartError(""); setConditionsWaiting(false);
      loaded.current = null; setContextPanel("scenario"); setSelectedObservation(null);
      if (savedRun && hasSavedRunEvidence(history, savedRun.run_id)) analyzed.current = savedRun.run_id;
      tabStorage.setItem(key, current.session_id);
    } catch (err) {
      if (stamp === generation.current) setError(err instanceof Error ? err.message : "Cannot reopen this workflow.");
    } finally {if (operation === busyOperation.current) setBusy(false);}
  }

  /** "Review response findings": ask the Simulation agent the run's crew question through the usual turn. */
  async function reviewFindings(question: string): Promise<void> {
    if (!runId) return;
    // ask() sends the current policy, so the review record keeps it for display and matching.
    const reviewed = runId, policyId = policy;
    const update = (change: (current: Review) => Review): void =>
      setReview(current => isReviewOf(current, reviewed, policyId) ? change(current) : current);
    setReview({ runId: reviewed, policyId, events: [], status: "running" });
    try {
      const output = await ask(question, "question", { onCustom: event =>
        update(current => ({ ...current, events: receiveSimulationActivity(current.events, event) })) });
      update(current => ({ ...current, status: "completed", turnId: output.turn_id,
        events: closeOpenEvents(current.events, "unverified") }));
    } catch (err) {
      update(current => ({ ...current, status: "failed", events: closeOpenEvents(current.events, "error") }));
      throw err;
    }
  }

  const radio = useLiveMonitoring({
    onDetected: async location => { await startScenario("radio", location); },
    onAudio: (url, file) => setAudio({ url, file }),
    onTranscript: setTranscript,
  });
  const chosen = result?.policies.find((p) => p.policy === policy);
  const frame: Frame | null = chosen?.frames?.filter((f: Frame) => f.t <= seconds).at(-1) || null;
  const candidateSeries = chosen?.series?.filter((s) => s.t <= seconds).at(-1);
  const series = result && candidateSeries && [candidateSeries.exited, candidateSeries.active, candidateSeries.not_departed,
    candidateSeries.crews_staged].every(n => Number.isInteger(n) && n >= 0) &&
    candidateSeries.exited + candidateSeries.active + candidateSeries.not_departed === result.scenario.vehicles &&
    candidateSeries.crews_staged <= result.scenario.crews ? candidateSeries : null;
  const point = incident?.location || null;
  const resolvedSearchArea = searchArea || (point?.origin === "geocoded_place" ? point.reported_address : null);
  const savedScenario = result?.scenario || draft?.scenario || run?.scenario ||
    (point ? { lat: point.lat, lon: point.lon, radius_km: radius, highway: "auto",
      hazard_radius_m: 150, duration_minutes: 30 } : null);
  const scenario = savedScenario ? { ...savedScenario, highway: savedScenario.highway ?? "auto" } : null;
  const selectedCorridor = scenario ? result?.network.corridor_label || draft?.network_validation?.corridor_label ||
    corridorLabel(scenario.highway, result?.network.selected_route) : null;
  const crews = chosen && result ? crewRecords(chosen, result.scenario.duration_minutes * 60) : null;
  const suggestions = caps?.operations.filter(op => op.enabled && op.suggested_question).slice(0, 5) || [];
  const crewQuestion = caps?.operations.find(op => op.id === "crew_details" && op.enabled)?.suggested_question ?? null;
  const liveReview = isReviewOf(review, runId, policy) ? review : null;
  // The live review's answer, or else the latest saved answer to the same question about this run whose policy-bearing
  // facts all name this policy.
  const reviewAnswer = liveReview ? messages.find(message => message.turn_id === liveReview.turnId) ?? null
    : runId && crewQuestion ? [...messages].reverse().find(message => message.question === crewQuestion
      && citesRun(message, runId) && citesOnlyPolicy(message, policy)) ?? null : null;
  const workingAgents = [investigation.initial, investigation.updated].flatMap(investigationActivity)
    .filter(event => event.status === "running" || event.status === "awaiting_result");
  const guarded = (action: () => Promise<unknown>): void => {
    const stamp = generation.current, operation = ++busyOperation.current;
    setBusy(true); setError("");
    action().catch(err => {if (stamp === generation.current) setError(err instanceof Error ? err.message : "Request failed.");})
      .finally(() => {if (operation === busyOperation.current) setBusy(false);});
  };
  const selectPolicy = (nextPolicy: string, replayTime: number | null = seconds): void => {
    if (!result?.policies.some((candidate) => candidate.policy === nextPolicy)) return;
    setPolicy(nextPolicy);
    setPlaying(false);
    if (replayTime !== null) setSeconds(replayTime);
    setFocusedCrew(null);
    setFocusRequest({kind: "network", changed: Date.now()});
  };
  const inspectFinding = (selected: string, view: "overview" | "bottlenecks" | "crews"): void => {
    const target = result?.policies.find(p => p.policy === selected);
    if (!target || !result) return;
    setPanel(view);
    const t = view === "bottlenecks" && target.series.length
      ? peakSample(target.series).t : result.scenario.duration_minutes*60;
    selectPolicy(selected, t);
    mapContainer.current?.scrollIntoView({behavior:"smooth", block:"start"});
  };
  /** Pause, focus a crew (or its planned origin) on the map and bring the map into view. */
  const locateCrew = (id: string, origin?: boolean): void => {
    setPlaying(false); setFocusedCrew(id); setFocusRequest({kind: origin ? "crew-origin" : "crew", id, changed: Date.now()});
    mapContainer.current?.scrollIntoView({behavior: "smooth", block: "start"});
  };
  // Stage 3 always shows the crew view of the selected run; stage 2's own panel choice is kept for its return.
  const view = responseStage ? "crews" : panel;

  return <main data-testid="guided-scenario" className={`evac-workspace ${expanded ? "expanded" : ""}`}>
    {error && <div className="evac-error" role="alert">{error}</div>}
    {accessFailed && <button onClick={() => setAccessAttempt(value => value + 1)}>Reconnect to workspace</button>}
    {(artifactFailedId === runId || networkFailedId === draft?.draft_id ||
      statusFailedId === runId || statusFailedId === draft?.draft_id) &&
      <button onClick={() => { setArtifactFailedId(null); setNetworkFailedId(null); setStatusFailedId(null);
        setReadAttempt(value => value + 1); }}>
        {artifactFailedId === runId ? "Load saved simulation results again" :
          networkFailedId === draft?.draft_id ? "Load prepared roads again" : "Retry status read"}
      </button>}
    {!ready || !restored ? <p role="status">{ready ? "Restoring saved workflow…" : "Connecting to the simulation workspace…"}</p> : <>
      {incident && !responseStage && <ol className="workflow-progress" aria-label="Scenario progress">
        <li><b>Location verified</b><span>{incident.location.provider} receipt saved</span></li>
        <li data-status={investigation.initial?.status}><b>Conditions &amp; initial plan</b><span>{conditionsWaiting
          ? "Waiting for earlier investigation" : investigation.initial?.status || "Awaiting investigation"}</span></li>
        <li data-status={draft?.status}><b>Road preparation · parallel</b><span>{preparationError ? "Awaiting retry"
          : draft?.status === "unavailable" ? "Road coverage unavailable" : draft?.status || (run ? "Reviewed and submitted" : "Not started")}</span></li>
        <li data-status={run?.status}><b>Traffic simulation</b><span>{run?.status || "Awaiting your draft review"}</span></li>
        <li data-status={investigation.updated?.status}><b>Updated response plan</b><span>{investigation.updated?.status || "Awaiting completed run evidence"}</span></li>
      </ol>}
      {incident && !responseStage && <div className="workflow-current" role="status">
        {workingAgents.length ? `${workingAgents.length} active ${workingAgents.length === 1 ? "action" : "actions"} · ${
          [...new Set(workingAgents.map(event => event.label))].join(" · ")}`
          : "No investigation tools running. Plans and activity are available below the map."}
        {draft?.status === "ready" && contextPanel !== "scenario" &&
          <button onClick={() => setContextPanel("scenario")}>Review ready draft</button>}
      </div>}
      <div className="evac-columns">
        <section ref={mapContainer} role={expanded ? "dialog" : undefined} aria-modal={expanded || undefined}
          aria-label="Evacuation map" className={`evac-viewer ${expanded ? "map-undocked" : ""}`}>
          <div className="evac-map-toolbar"><div>{point?.matched_address || "Choose a scenario to begin"}</div>
            <div className="evac-map-toolbar-actions">
              {result && !responseStage && <button aria-pressed={panel === "evidence"} onClick={() => {
                setPanel("evidence"); setPlaying(false);
              }}>Inspect source data</button>}
              {!!observations.length && !responseStage && <button onClick={() => {setContextPanel("conditions"); setObservationFocus(value => value+1);}}>Show nearby fires &amp; weather</button>}
              {expanded && audio && <button onClick={() => {
                const player = radioPlayer.current;
                if (player?.paused) void player.play().catch(() => setError("Radio playback is unavailable."));
                else player?.pause();
              }}>{radioPlaying ? "Pause radio recording" : "Play radio recording"}</button>}
              <button data-map-fullscreen aria-expanded={expanded} onClick={toggleMap} title="Detach map and enter full screen; Escape restores it">
                {expanded ? "Restore map" : "Enlarge map"}</button>
            </div>
          </div>
          {scenario ? <SimulationMap preparedGates={gateCoordinates(draft?.network_validation?.gates)}
            showStudyArea={!!result || !!draft?.network_validation?.study_bounds?.length}
            studyBounds={draft?.network_validation?.study_bounds} scenario={scenario} network={network} frame={frame}
            result={result} observations={observations} observationFocus={observationFocus}
            policy={chosen} sources={result?.sources} focus={view} selectedObservation={selectedObservationId}
            onSelectObservation={id => {setSelectedObservation(id); setContextPanel("conditions"); setPlaying(false);}} focusRequest={focusRequest}
            showRoutes={showRoutes} showTrails={showTrails} showTraffic={showTraffic} showCongestion={showCongestion}
            onInspect={() => setPlaying(false)}/> : <div className="evac-empty" role="status">
              Resolve a U.S. scenario location to view its study area. No fire point or road corridor is assumed.</div>}
          {!responseStage && <nav className="scenario-nav" aria-label="Incident details">
            {(["overview", "bottlenecks", "crews", "evidence", "plans", "activity"] as const).map(value =>
              <button key={value} aria-pressed={panel === value}
                disabled={!result && value !== "plans" && value !== "activity"} onClick={() => {
                setPanel(value); if (value === "evidence") setPlaying(false);
              }}>{{ overview: "Overview", bottlenecks: "Bottlenecks", crews: "Fire crews", evidence: "Data & evidence", plans:"Response plans", activity:"Agent activity" }[value]}</button>)}</nav>}
          {view !== "evidence" && <ReplayStatus result={result} frame={frame} playing={playing}/>}

          {result ? <>
            <div className="evac-replay">{responseStage ? <ReplayedPolicy policy={policy}/>
              : <div>{result.policies.map((p) =>
              <button key={p.policy} aria-pressed={policy === p.policy}
                onClick={() => policy !== p.policy && selectPolicy(p.policy)}>{policyNames[p.policy]}</button>)}</div>}
              <button aria-label={playing ? "Pause replay" : "Play replay"} onClick={() => {
                if (seconds >= result.scenario.duration_minutes * 60) setSeconds(0);
                setPlaying(!playing);
              }}>{playing ? "Pause" : "Play"}</button>
              <label>Elapsed time {clock(seconds)}<input type="range" min="0" max={result.scenario.duration_minutes * 60}
                step="1" value={seconds} onChange={e => { setPlaying(false); setSeconds(Number(e.target.value)); }} /></label>
              <label>Playback speed<select aria-label="Playback speed" value={speed} onChange={e => setSpeed(Number(e.target.value))}>
                {[5,20,60,120].map(value => <option key={value} value={value}>{value}×</option>)}</select></label>
              <small>Recorded sample: {frame ? clock(frame.t) : "Unavailable"} · no interpolation</small>
            </div>
            {!responseStage && <div className="situation-controls" role="group" aria-label="Focus on a situation">
              <span>Jump to:</span>
              <button onClick={() => {setPlaying(false); setSeconds(0);}}>Start</button>
              <button onClick={() => {setPlaying(false); setSeconds(result.scenario.hazard_start_minutes*60);}}>Hazard begins</button>
              <button disabled={!chosen?.series?.length} onClick={() => {
                setPlaying(false); setPanel("bottlenecks");
                if (chosen) setSeconds(peakSample(chosen.series).t);
              }}>Peak slowdown</button>
              <button onClick={() => {setPlaying(false); setSeconds(result.scenario.duration_minutes*60);}}>End of run</button>
              <button onClick={() => setFocusRequest({kind:"hazard",changed:Date.now()})}>Focus on hazard</button>
              <button onClick={() => setFocusRequest({kind:"network",changed:Date.now()})}>Whole study area</button>
              {result.traffic?.status === "applied" && <label><input type="checkbox" checked={showTraffic}
                onChange={e => setShowTraffic(e.target.checked)}/> Show saved traffic speeds</label>}
              <label><input type="checkbox" checked={showCongestion}
                onChange={e => setShowCongestion(e.target.checked)}/> Show congestion</label>
              <label><input type="checkbox" checked={showRoutes}
                onChange={e => setShowRoutes(e.target.checked)}/> Show crew routes</label>
              <label><input type="checkbox" checked={showTrails}
                onChange={e => setShowTrails(e.target.checked)}/> Show crew trails</label>
            </div>}
            <div className="evac-replay-totals" aria-live="polite"><b>At this replay time</b>
              {series ? <p>{series.exited * result.scenario.people_per_vehicle} people at model exits ·
                {" "}{(series.active + series.not_departed) * result.scenario.people_per_vehicle} unresolved ·
                {" "}{series.not_departed * result.scenario.people_per_vehicle} not departed ·
                {" "}{series.crews_staged} of {result.scenario.crews} crews staged</p> : <p>Replay counts unavailable.</p>}</div>
            {!["evidence", "plans", "activity"].includes(view) && <MetricCards items={view === "crews" ? [
                ["Crews on roads", frame?.vehicles.filter(v => v[4]).length, "Recorded crew vehicles at this replay time."],
                ["Crews staged", series?.crews_staged, "Reached assigned staging; not completed suppression."],
                ["Crews returned", crewsReturned(frame, chosen, result.scenario.duration_minutes * 60),
                  "Returned to a model exit after staging."],
              ] : view === "bottlenecks" ? [
                ["Slow / stopped", series?.queue, "On-road vehicles below about 1.1 mph, including crew stops."],
                ["Not departed", series?.not_departed, "Waiting before road entry, including held or unroutable demand."],
                ["Closed segments", series?.closed_edges, "Closed to new entry by the assumed hazard."],
              ] : [
                ["Model exits", series?.exited, "Civilian vehicles at assumed model exits."],
                ["Still on roads", series?.active, "Vehicles in the network, including queues."],
                ["Not departed", series?.not_departed, "Includes held or unroutable civilian demand."],
                ["Crews staged", series?.crews_staged, "At assigned staging; not completed suppression."],
              ]}/>}
            {responseStage ? <ReplayCrews result={result} frame={frame} showRoutes={showRoutes} showTrails={showTrails}
              setRoutes={setShowRoutes} setTrails={setShowTrails} locate={locateCrew} replayPolicy={chosen}/>
            : !["plans", "activity"].includes(panel) && <div className="evac-details">
              {panel === "overview" && <><MissionFindings result={result} inspect={inspectFinding}/>
                <details><summary>Detailed resident and crew comparison</summary><MissionComparison result={result}/></details>
                <details><summary>Recorded outcomes and export</summary>
                  <ReplayExplanation result={result} policy={chosen} frame={frame} metrics={series}/>
                  <Comparison result={result} index={result.policies.findIndex((p) => p.policy === policy)}
                    setIndex={(index:number) => selectPolicy(result.policies[index].policy)}/></details></>}
              {panel === "bottlenecks" && <><BottleneckExplanation result={result} policy={chosen} frame={frame}/>
                <PolicyInsights result={result} inspect={(index: number, t: number) => {
                selectPolicy(result.policies[index].policy, t);
              }} /></>}
              {panel === "crews" && <><h3>Fire crews · movement and access</h3>
                <CrewExplanation result={result} frame={frame} showRoutes={showRoutes} showTrails={showTrails}
                  setRoutes={setShowRoutes} setTrails={setShowTrails} locate={locateCrew}/>
                <p>Every assigned crew remains visible, including those that never reach staging.</p>
                {crews ? <details><summary>Full-run crew timeline</summary><table><thead><tr><th>Crew</th><th>Scheduled</th><th>Entered roads</th><th>Reached staging</th><th>Returned</th></tr></thead>
                  <tbody>{crews.map(c => <tr key={c.id} className={focusedCrew === c.id ? "focused-crew" : ""}><th><button onClick={() => setFocusedCrew(c.id)}>{c.id}</button></th>
                    {[c.scheduled_departure_s, c.departed_s, c.reached_staging_s, c.returned_s].map((v, i) =>
                      <td key={i}>{v == null ? "Not reached" : clock(v)}</td>)}</tr>)}</tbody></table></details> : <p>Complete crew timing unavailable.</p>}
                <MissionComparison result={result} crewOnly/></>}
              {panel === "evidence" && <>
                <div id="simulation-provider-readings"><ProviderReadings traffic={result.traffic}
                  highway={selectedCorridor || undefined} locate={(segment, number: number) => {
                    setPlaying(false); setShowTraffic(true);
                    setFocusRequest({kind: "traffic-segment", id: segment.id, number,
                      highway: selectedCorridor || undefined, changed: Date.now()});
                    window.requestAnimationFrame(() => document.getElementById(REPLAY_MAP_ID)
                      ?.scrollIntoView({behavior: "smooth", block: "center"}));
                  }}/></div>
                <RunEvidence result={result} job={{id:run?.run_id,status:run?.status}}/>
                <TrafficEvidence traffic={result.traffic}/>
                {result.sources?.sources?.map((source, index: number) => <SourceDetails source={source} key={index} />)}
                <h3>Scenario assumptions</h3><ul>{result.assumptions.map((a: string) => <li key={a}>{a}</li>)}</ul>
                <p>Input bundle: {result.input_bundle_id || "Unavailable"} · {result.scenario.vehicles} assumed vehicles,
                  {" "}{result.scenario.people_per_vehicle} people per vehicle; {result.scenario.crews} assumed crews.</p>
              </>}
              <details className="map-guide-disclosure"><summary>Map symbols · why red roads can extend beyond the hazard circle</summary>
                <MapGuide sources={result.sources} scenario={result.scenario}/></details>
              <details><summary>Compare a controlled change</summary><p>New draft, same frozen roads, traffic, hazard and crew assignments.</p>
                <label>Vehicles<input type="number" min="20" max="1500" value={variantVehicles}
                  onChange={e => setVariantVehicles(Number(e.target.value))} /></label>
                <label>Departure window (minutes)<input type="number" min="1" max="30" value={variantDeparture}
                  onChange={e => setVariantDeparture(Number(e.target.value))} /></label>
                <button disabled={busy || !caps?.operations.some(o => o.id === "variant_vehicle_count" && o.enabled)}
                  onClick={() => guarded(() => ask(`Prepare a frozen-input variant with ${variantVehicles} vehicles and a ${variantDeparture}-minute departure window. Do not submit it yet.`))}>
                  Propose comparison</button></details>
            </div>}
          </> : responseStage ? <NoReplay run={run}/>
            : !["plans", "activity"].includes(panel) ? <div className="evac-empty"><h2>Follow the investigation from location to outcomes</h2>
            <p>{run?.status === "failed" ? "This run failed. No complete comparison is available; prepare a new validated area." :
              draft?.status === "preparing" ? "The worker is checking road size and modeled exits. No policy simulation has started." :
              draft?.status === "ready" ? "Review the validated extent and modeled exits, then run the draft." :
              run && active(run) ? "The worker is capturing current inputs and running the policy comparisons." :
              "Radio Investigation and Simulation Lab use the same simulation and evidence tools."}</p>
            <p>The map will show residents, crews and the assumed hazard once a complete run is available.</p></div> : null}
          {!responseStage && (panel === "plans" || panel === "activity") && <div className="evac-details">
            <IncidentInvestigation section={panel} initial={investigation.initial} updated={investigation.updated}
              result={result} runId={runId} inspect={inspectFinding}
              error={investigation.error} onRetry={selectedRun => {
                const stamp = generation.current;
                if (incident) void investigation.start(incident.incident_id, selectedRun, true).catch(err => {
                  if (stamp === generation.current) setError(err.message);
                });
              }} onCancel={selectedRun => {
                const stamp = generation.current;
                void investigation.cancel(selectedRun).catch(err => {if (stamp === generation.current) setError(err.message);});
              }} />
          </div>}
        </section>
        {responseStage && <ResponseTrackerActions question={crewQuestion} disabled={busy || !result} review={liveReview}
          answer={reviewAnswer} onReview={() => { if (crewQuestion) guarded(() => reviewFindings(crewQuestion)); }}
          exerciseOpen={exerciseOpen} onOpenExercise={onOpenExercise} />}
        {/* Stage 2's context stays mounted in stage 3, so its radio audio and inputs survive. */}
        <aside className="evac-investigation" aria-label="Incident context" hidden={responseStage}>
          <nav className="context-tabs" aria-label="Incident context views">
            {(["scenario", "conditions", "assistant"] as const).map(value => <button key={value}
              aria-pressed={contextPanel === value} aria-controls={`incident-context-${value}`} onClick={() => setContextPanel(value)}>
              {{scenario:"Scenario", conditions:"Conditions", assistant:"Ask agent"}[value]}</button>)}
          </nav>
          {audio && <section className="radio-recording"><h3>Radio recording</h3>
            <audio ref={radioPlayer} src={audio.url} autoPlay controls aria-label="Prerecorded emergency radio"
              onPlay={() => setRadioPlaying(true)} onPause={() => setRadioPlaying(false)} onEnded={() => setRadioPlaying(false)} />
            <details><summary>Recording source</summary><p>{audio.file}</p></details>
          </section>}
          <div id="incident-context-scenario" hidden={contextPanel !== "scenario"} className="context-view">
      <div className="evac-modes" role="tablist" aria-label="Scenario entry">
        {(["radio", "lab"] as const).map(value => <button role="tab" aria-selected={mode === value}
          disabled={busy || radio.isMonitoring} key={value} onClick={() => setMode(value)}>
          {value === "radio" ? "Radio Investigation" : "Reference places"}</button>)}
      </div>

          {mode === "lab" ? <section><h2>A historical place. Today’s inputs.</h2>
            <label>Scenario reference<select value={preset} onChange={e => setPreset(e.target.value)} disabled={busy}>
              <option value="paradise-2018">Paradise — Camp Fire (2018)</option>
              <option value="palisades-2025">Pacific Palisades, Los Angeles — Palisades Fire (2025)</option>
            </select></label>
            <p>Run a new experiment at a representative location using current road and traffic inputs. This is not a historical reconstruction.</p>
            <button disabled={busy || draft?.status === "preparing"} onClick={() => guarded(() => startScenario("lab", preset))}>Prepare with current data</button>
          </section> : <section><h2>From radio report to response planning</h2>
            <p>Play the recording, verify its reported address, and follow fire, weather and guidance agents while roads are prepared.</p>
            <button disabled={busy || radio.isMonitoring} onClick={() => radio.startMonitoring()}>Play radio &amp; investigate</button>
            {radio.isMonitoring && <button onClick={() => radio.stopMonitoring()}>Stop listening</button>}
            {transcript && <details open><summary>Detected radio report</summary><blockquote>{transcript}</blockquote></details>}
            {radio.detection && <p><b>Reported address:</b> {radio.detection.location ||
              "Fire-related audio detected; awaiting an address."}</p>}
            {!!radio.logs.length && <div aria-live="polite"><RadioStatus radio={radio}/>
              <details><summary>Radio processing details</summary>
                <ol>{radio.logs.map((entry, index) => <li key={index}>{entry.time} · {entry.message}</li>)}</ol>
              </details></div>}
            <details><summary>Enter or correct a reported address</summary>
              <input aria-label="Reported address" value={address} onChange={e => setAddress(e.target.value)}
                placeholder="Street address or intersection, city, state, United States" />
              <button disabled={busy || address.length < REPORTED_ADDRESS_MIN_LENGTH}
                onClick={() => guarded(() => startScenario("radio", address, { from: "typed-address" }))}>
                Resolve &amp; prepare</button></details>
            {radio.errorMessage && <p role="alert">{radio.errorMessage}</p>}
            <CoordinateEntry busy={busy} onResolve={coordinates => guarded(() => startScenario("radio",
              "Entered scenario coordinate", {from:"typed-address",input:{coordinates}}))} />
          </section>}
          {point && <section className="resolved-location">
            <h3>{resolvedSearchArea ? "Verified search area" : "Verified scenario point"}</h3>
            {resolvedSearchArea && <p>Search area from 1 · Investigate: {resolvedSearchArea}. No specific fire was selected.</p>}
            <b>{point.matched_address}</b><p>{point.lat.toFixed(5)}, {point.lon.toFixed(5)} · WGS84</p>
            <small>{point.provider} {point.address_type}{point.score != null ? ` · match score ${point.score}` : ""}
              {" · "}{observationTime(point.resolved_at)}. {point.meaning}</small>
            {point.source_timestamp && <p>Fire source time: {observationTime(point.source_timestamp)}.</p>}
            <p>The simulation hazard is assumed at this point; reported fire observations do not drive its growth.</p></section>}
          {pendingStart && <section className="evac-draft" aria-label="Pending workflow start"><h3>
            {pendingStart.incident ? "Road preparation awaiting retry" : "Workflow start awaiting retry"}</h3>
            <p>{pendingStart.label} · {preparationError || "Resume the saved request to recover its acknowledgement."}</p>
            <p>{pendingStart.geocoderOutcomeUnknown ? "The paid lookup outcome is unknown. This request will not invoke it again."
              : pendingStart.incident ? "The verified incident is retained. Retry only road preparation when capacity is available."
              : "The same saved request identities are reused; an acknowledged session is retained."}</p>
            {!resumePermitted && pendingStart.retryAtMs && <p role="status">The service requests retry after
              {" "}{new Date(pendingStart.retryAtMs).toLocaleTimeString()}.</p>}
            <button disabled={busy || !resumePermitted} onClick={() => {
              const intent = pendingStart.geocoderOutcomeUnknown
                ? createWorkflowStart(pendingStart.entryMode, pendingStart.source, pendingStart.label) : pendingStart;
              void continueWorkflow(intent).catch(() => undefined);
            }}>
              {pendingStart.geocoderOutcomeUnknown ? "Start a new paid location lookup" :
                pendingStart.incident ? "Resume road preparation" : "Resume workflow start"}</button>
          </section>}
          {incident && !investigation.initial && <section><h3>Incident investigation</h3>
            <p>Start the fire, weather and guidance agents for this verified location.</p>
            {conditionsStartError && <p role="status">{conditionsWaiting
              ? "An earlier initial investigation is still running for this demo session. Roads can proceed independently. Resume conditions after it finishes."
              : conditionsStartError}</p>}
            <button disabled={busy} onClick={() => {void beginConditions(incident);}}>
              {conditionsStartError ? "Resume conditions" : "Investigate conditions & plan"}</button></section>}
          {incident && runStatus === "completed" && !investigation.updated && <section><h3>Update the response plan</h3>
            <p>Use this run’s frozen sources and resident/crew evidence.</p>
            <button onClick={() => {
              const stamp = generation.current;
              void investigation.start(incident.incident_id, runId).catch(err => {if (stamp === generation.current) setError(err.message);});
            }}>
              Build simulation-informed plan</button></section>}
          {!!runs.length && <label>Retained runs<select value={run?.run_id || ""} disabled={busy}
            onChange={event => {
              const selected = runs.find(value => value.run_id === event.target.value);
              if (selected) void rejoinWorkflow({sessionId:selected.session_id,incidentId:selected.incident_id,
                label:"Retained simulation"}, selected);
            }}><option value="">Choose a run</option>{runs.map(r => <option key={r.run_id} value={r.run_id}>
              {new Date(r.created_at).toLocaleString()} · {r.status} · {r.scenario.vehicles} vehicles</option>)}</select></label>}
          {!!retainedWorkflows.length && <label>Retained workflows<select value={sessionId || ""} disabled={busy}
            onChange={event => {
              const selected = retainedWorkflows.find(value => value.sessionId === event.target.value);
              if (selected) void rejoinWorkflow(selected);
            }}><option value="">Choose a workflow</option>{retainedWorkflows.map(value =>
              <option key={value.sessionId} value={value.sessionId}>{value.label} · {value.sessionId.slice(0,8)}</option>)}
          </select></label>}
          {draft && <section className="evac-draft"><h3>{draft.status === "preparing" ? "Preparing road network"
            : draft.status === "failed" ? "Preparation failed" : draft.status === "unavailable"
              ? "Road coverage unavailable" : "Review scenario draft"}</h3>
            {draft.status === "failed" ? <>
              <p role="alert">{draft.error || "Road preparation failed. No simulation was started."}</p>
              <p>This saved preparation attempt failed. Reloading keeps its result; it does not retry the road download.</p>
              <p>Retry this verified incident after the reported service issue is resolved, or select another reported fire in Conditions.</p>
              {incident && <button disabled={busy} onClick={() => {void prepareAcceptedArea().catch(() => undefined);}}>
                Retry road preparation</button>}
            </> : draft.status === "unavailable" ? <>
              <p role="status">{draft.road_coverage?.message || draft.error || "No usable road network was found for this study area."}</p>
              <p>Select another fire in Conditions{draft.road_coverage?.extent_mode === "fixed"
                ? ", or request automatic road discovery for this verified point." : ". Automatic discovery has already checked the allowed extents."}</p>
              {draft.road_coverage?.extent_mode === "fixed" && <button disabled={busy}
                onClick={() => {void prepareAcceptedArea().catch(() => undefined);}}>Discover roads automatically</button>}
            </> : <p role="status">{draft.phase}</p>}
            {draft.road_coverage && <details><summary>Road coverage evidence</summary>
              <p>{draft.road_coverage.reason_code} · {draft.road_coverage.extent_mode === "auto"
                ? "Automatic study extent" : "Requested study extent"} · searched to {miles(draft.road_coverage.searched_radius_km)}</p>
              <p>{draft.road_coverage.attempts?.length || 0} documented preparation attempts.</p>
            </details>}
            <p>{draft.scenario.vehicles} vehicles · {draft.scenario.departure_minutes}-minute departures · {selectedCorridor}</p>
            <p>Study radius: <b>{miles(draft.scenario.radius_km)}</b> · {!draft.scenario.exit_direction || draft.scenario.exit_direction === "auto"
              ? "Exit selection pending." : `assumed exits face ${draft.scenario.exit_direction}.`}</p>
            <p>{draft.scenario.traffic_mode === "synthetic" ? "Assumed traffic; demand and road speeds are scenario inputs."
              : draft.scenario.traffic_mode === "provider_speed_limits"
                ? "Provider failures or unusable speeds stop this run. If the shared daily request budget is exhausted, the result explicitly reports unavailable provider speeds and uses OSM road limits."
                : "Traffic input mode is unavailable in this saved draft."}</p>
            {draft.network_validation && <div className="network-validation">
              {draft.network_validation.extent_mode === "auto" ? <p><b>Road-aware study extent:</b>
                {" "}{miles(draft.network_validation.effective_radius_km)} selected from the checked road network.
                {" "}{draft.network_validation.attempts.length} documented area checks. Review the boundary and modeled exits.</p>
                : draft.network_validation.adjusted && <p><b>{draft.network_validation.effective_radius_km <
                  draft.network_validation.requested_radius_km ? "Smaller area proposed:" : "Adjusted study area:"}</b>
                  {" "}changed from {miles(draft.network_validation.requested_radius_km)} to {miles(draft.network_validation.effective_radius_km)}
                  {" "}to fit the road-network limits. This changes the study boundary and modeled exits.</p>}
              <p>{draft.network_validation.edge_count.toLocaleString()} eligible road segments · {draft.network_validation.gate_count} modeled exits. Exit safety is unverified.</p>
              {draft.network_validation.crew_staging_distance_km != null && <p>Road-based crew staging is
                {" "}{miles(draft.network_validation.crew_staging_distance_km)} from the fire point.
                The off-road last mile is unverified; this simulation covers road access.</p>}
            </div>}
            <p>{draft.input_mode === "frozen_parent" ? "Reuses the parent’s frozen inputs." : "Uses the validated road snapshot; captures current traffic and context when run."}</p>
            {draft.status !== "failed" && draft.status !== "unavailable" && <button disabled={busy || draft.status !== "ready"} onClick={() => guarded(() => ask("Run this reviewed scenario draft.", "submit"))}>Run this draft</button>}
            {draft.status === "preparing" && <button onClick={() => guarded(async () => setDraft(await api<DraftResponse>(
              `/scenario-drafts/${draft.draft_id}/cancel`, {request_id: crypto.randomUUID()})))}>Cancel preparation</button>}
          </section>}
          {incident && !active(run) && draft?.status !== "preparing" && <details><summary>Prepare a different study area</summary>
            <UnitInput label="Requested network radius · miles" value={radius} factor={MILES_PER_KM} min={2} max={15} onChange={value => {if (typeof value === "number") setRadius(value);}} />
            <p>A new area is a new scenario. Existing runs and frozen-input comparisons keep their original extent.</p>
            <button disabled={busy} onClick={() => guarded(async () => {
              await prepareAcceptedArea(radius);
            })}>Validate new area</button>
          </details>}
          <section className="evac-activity" aria-live="polite"><h3>Investigation activity</h3>
            {!!activity.length && <><p>{activity[activity.length - 1]}</p>
              <details><summary>View agent activity</summary>
                <ol>{activity.map((text, index) => <li key={`${index}-${text}`}>{text}</li>)}</ol>
              </details></>}
            {run && <><b>{run.phase}</b><progress max="1" value={run.progress} />
              <p>{run.status} · Run {run.run_id.slice(0, 8)}</p>
              {active(run) && <button disabled={run.status === "cancelling"} onClick={() => guarded(async () =>
                setRun(await api<RunResponse>(`/runs/${run.run_id}/cancel`, { request_id: crypto.randomUUID() })))}>
                Cancel simulation</button>}</>}
          </section>
          </div>
          <div id="incident-context-conditions" hidden={contextPanel !== "conditions"} className="context-view">
            <IncidentConditions task={investigation.initial} observations={observations} point={point || undefined}
              busy={busy} startFire={selected => {
                if (selected.sourceObservationId) void startScenario("radio", selected.name,
                  {from:"reported-fire",input:{fire_observation_id:selected.sourceObservationId}}).catch(() => undefined);
              }}
              selectedId={selectedObservationId} select={id => {
                setSelectedObservation(id); setPlaying(false); setFocusRequest({kind:"observation", id, changed:Date.now()});
              }} />
          </div>
          <div id="incident-context-assistant" hidden={contextPanel !== "assistant"} className="context-view">
          <section className="evac-conversation"><h2>Ask about this simulation</h2>
            <p>Selected context: {policyNames[policy]} · {clock(seconds)} elapsed.</p>
            <div className="evac-message-history" role="region" aria-label="Simulation conversation history" tabIndex={0}>
            {messages.map(message => <SimulationAnswer key={message.turn_id} message={message}/>)}
            </div>
            <div className="evac-suggestions">{suggestions.map(op => <button key={op.id} disabled={busy || !result}
              onClick={() => { setQuestion(op.suggested_question!); }}>{op.suggested_question}</button>)}</div>
            <form onSubmit={e => { e.preventDefault(); const text = question; setQuestion("");
              guarded(() => ask(text)); }}>
              <label htmlFor="simulation-question">Question</label><textarea id="simulation-question" value={question}
                onChange={e => setQuestion(e.target.value)} placeholder="Compare evacuation and crew access, inspect a crew, or propose a supported change…" />
              <button disabled={busy || !incident || !question.trim()}>Ask simulation agent</button></form>
            {busy && <p role="status">Working with simulator tools…</p>}

          </section>
          </div>
        </aside>
      </div>
    </>}
  </main>;
}
