import useChat, { type Message } from "@/features/chat/hooks/useChat";
import type { LocationData, ParsedResponse } from "@/shared/services/agentResponseParser";
import type { MapData, CenterLocation } from "@/features/map/types/types";
import { useResponseRegistryContext } from "@/features/reports/ResponseRegistryContext";
import { MESSAGES,TIMING } from "@/shared/constants";
import { invokeAgentStreaming, resumeAgentInvestigation, type InvokeAgentStreamingCallbacks,
  type InvokeAgentStreamingHandle } from "@/shared/services/AgentService";
import { newInvestigation,pendingInvestigation,type PendingInvestigation } from "@/shared/services/investigationRecovery";
import { logger } from "@/shared/utils/logger";
import {
  closeOpenEvents, endSupervisorRun, endToolArguments, receiveToolResult, startSupervisorRun, startToolEvent,
  type SupervisorRun, type ToolEvent,
} from "@/shared/services/toolActivity";
import { useCallback,useEffect,useRef,useState } from "react";


// ============================================================================
// HOOK INTERFACE
// ============================================================================

interface UseAgentSubmissionParams {
  onEvidence: (data: ParsedResponse) => void;
}

interface UseAgentSubmissionReturn {
  toolEvents: ToolEvent[];
  /** The supervisor run from RUN_STARTED to its terminal signal; undefined until RUN_STARTED arrives. */
  supervisorRun: SupervisorRun | undefined;
  // Chat state
  messages: Message[];
  isLoading: boolean;
  inputText: string;
  /**
   * Live status string driven entirely by AG-UI events. Starts at
   * `MESSAGES.THINKING` and is overwritten by `TOOL_CALL_START` labels as
   * the supervisor delegates to sub-agents and tools.
   */
  currentLoadingMessage: string;
  setInputText: (text: string) => void;

  // Map state
  mapData: MapData | null;
  centerLocation: CenterLocation | null;
  selectAnswer: (answer: ParsedResponse) => void;
  /** The selected answer's trimmed place name, which 2 · Evacuation & Crew Access can simulate as a search area. */
  searchArea: string | null;

  // Submit handler
  handleSubmit: (e: React.FormEvent) => Promise<void>;
  canResume: boolean;
  resumeAccepted: () => void;
}

/**
 * useAgentSubmission - Drives the chat → AgentCore (AG-UI streaming) flow.
 *
 * Owns:
 * - Chat message state (via useChat) including streaming bubble
 * - Input text + live loading message
 * - Map data + center location
 * - Orchestrating the AG-UI run
 */
export function useAgentSubmission({
  onEvidence,
}: UseAgentSubmissionParams): UseAgentSubmissionReturn {
  const {
    messages,
    isLoading,
    setIsLoading,
    addUserMessage,
    addErrorMessage,
    startStreamingMessage,
    replaceStreamingMessage,
    finalizeStreamingMessage,
  } = useChat();

  const [inputText, setInputText] = useState("");
  const [toolEvents, setToolEvents] = useState<ToolEvent[]>([]);
  const [supervisorRun, setSupervisorRun] = useState<SupervisorRun | undefined>();
  const [currentLoadingMessage, setCurrentLoadingMessage] = useState<string>(
    MESSAGES.THINKING,
  );

  const [mapData, setMapData] = useState<MapData | null>(null);
  const [centerLocation, setCenterLocation] = useState<CenterLocation | null>(null);
  const activeRequest = useRef(0);
  const selectionRevision = useRef(0);
  const mounted = useRef(true);
  const activeHandle = useRef<InvokeAgentStreamingHandle | null>(null);
  const safetyTimer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  const restored = useRef(false);
  const [canResume, setCanResume] = useState(false);

  const { registerFires } = useResponseRegistryContext();
  const selectAnswer = useCallback((answer: ParsedResponse): void => {
    selectionRevision.current += 1;
    const latitude = answer.location?.latitude ?? answer.location?.lat;
    const longitude = answer.location?.longitude ?? answer.location?.lon;
    // The answer is cast from agent JSON, whose place name the runtime copies from tool results: keep it only as text.
    const name = typeof answer.location?.name === "string" ? answer.location.name : undefined;
    const center = typeof latitude === "number" && typeof longitude === "number"
      ? {latitude, longitude, name} : null;
    setCenterLocation(center);
    setMapData({locations: answer.locations, fireExposureData: answer.fireExposureData,
      fireExposureJobs: answer.fireExposureJobs, fireExposureError: answer.fireExposureError});
    onEvidence(answer);
  }, [onEvidence]);

  const startSubmission = useCallback(
    (intent: PendingInvestigation, readOnly = false, includeQuestion = true): void => {
      const trimmed = intent.question;
      const requestId = ++activeRequest.current;
      const selectionAtStart = selectionRevision.current;

      if (includeQuestion) addUserMessage(trimmed);
      if (!readOnly) setInputText("");
      setIsLoading(true);
      setCanResume(false);
      setToolEvents([]);
      setSupervisorRun(undefined);
      setCurrentLoadingMessage(MESSAGES.THINKING);

      const streamingId = `agent-stream-${Date.now()}-${requestId}`;
      startStreamingMessage(streamingId);

      // Safety timeout in case the SSE stream never closes
      let timedOut = false;
      const safety = setTimeout(() => {
        timedOut = true;
        handle.cancel();
      }, TIMING.AGENT_TIMEOUT_MS);
      safetyTimer.current = safety;

      const callbacks: InvokeAgentStreamingCallbacks = {
        onRunStarted: () => {
          if (requestId === activeRequest.current) setSupervisorRun(startSupervisorRun());
        },
        onToolStart: (id, name, label) => {
          if (requestId === activeRequest.current) setToolEvents(events => startToolEvent(events, id, name, label));
        },
        onToolEnd: id => {
          if (requestId === activeRequest.current) setToolEvents(events => endToolArguments(events, id));
        },
        onToolResult: (id, content) => {
          if (requestId === activeRequest.current) setToolEvents(events => receiveToolResult(events, id, content));
        },
        onLoadingMessage: (msg) => {
          if (requestId === activeRequest.current) setCurrentLoadingMessage(msg);
        },
        onComplete: (response) => {
          clearTimeout(safety);
          if (!mounted.current) return;
          replaceStreamingMessage(streamingId, response.response || "");
          finalizeStreamingMessage(streamingId, response);
          if (requestId !== activeRequest.current || timedOut) return;
          setToolEvents(events => closeOpenEvents(events, "unverified"));
          setSupervisorRun(run => endSupervisorRun(run, "completed"));
          // A completed answer keeps its evidence without displacing an answer selected while it was loading.
          if (selectionRevision.current === selectionAtStart) selectAnswer(response);
          registerFiresFromLocations(response.locations);
          setIsLoading(false);
          setCanResume(false);
          setCurrentLoadingMessage(MESSAGES.THINKING);
        },
        onError: (err) => {
          clearTimeout(safety);
          if (!mounted.current) return;
          if (requestId !== activeRequest.current) {
            finalizeStreamingMessage(streamingId);
            replaceStreamingMessage(streamingId, "");
            return;
          }
          setToolEvents(events => closeOpenEvents(events, "error"));
          setSupervisorRun(run => endSupervisorRun(run, "failed"));
          if (timedOut) {
            addErrorMessage(new Error(MESSAGES.TIMEOUT_ERROR));
          } else {
            logger.error("Agent invocation error:", err);
            addErrorMessage(
              err instanceof Error ? err : new Error(String(err)),
            );
          }
          finalizeStreamingMessage(streamingId);
          // If the streaming bubble never received text, replace with empty
          replaceStreamingMessage(streamingId, "");
          setIsLoading(false);
          setCanResume(!!pendingInvestigation());
          setCurrentLoadingMessage(MESSAGES.THINKING);
        },
      };
      const handle = readOnly ? resumeAgentInvestigation(intent, callbacks)
        : invokeAgentStreaming(trimmed, callbacks, intent);
      activeHandle.current = handle;

      function registerFiresFromLocations(locs: LocationData[]      ): void {
        const fires = locs
          .filter((loc) => loc.fire)
          .map((loc) => ({
            fireId:
              `fire-${loc.fire?.name || "unnamed"}-${loc.longitude}-${loc.latitude}`,
            fireName: loc.fire?.name || "Unknown Fire",
            location: { lat: loc.latitude, lng: loc.longitude },
            acres: loc.fire?.acresBurned ?? undefined,
            containment: loc.fire?.percentContained ?? undefined,
            lastSeen: new Date().toISOString(),
          }));
        if (fires.length > 0) registerFires(fires);
      }
    },
    [
      addUserMessage,
      addErrorMessage,
      setIsLoading,
      registerFires,
      selectAnswer,
      startStreamingMessage,
      replaceStreamingMessage,
      finalizeStreamingMessage,
    ],
  );
  const handleSubmit = useCallback(async (e: React.FormEvent): Promise<void> => {
    e.preventDefault();
    const trimmed = inputText.trim();
    if (!trimmed) return;
    if (pendingInvestigation()) { setCanResume(true); return; }
    startSubmission(newInvestigation(trimmed));
  }, [inputText, startSubmission]);
  const resumeAccepted = useCallback((): void => {
    const intent = pendingInvestigation();
    if (intent) startSubmission(intent, true, false);
    else setCanResume(false);
  }, [startSubmission]);
  const startSubmissionRef = useRef(startSubmission);
  startSubmissionRef.current = startSubmission;
  const invalidateRequest = useCallback((): void => { activeRequest.current += 1; }, []);
  useEffect(() => {
    mounted.current = true;
    const intent = pendingInvestigation();
    // StrictMode may stop and set up this effect again: restart only the read, with one saved question bubble.
    if (intent) startSubmissionRef.current(intent, true, !restored.current);
    restored.current = true;
    return () => {
      mounted.current = false;
      invalidateRequest();
      clearTimeout(safetyTimer.current);
      activeHandle.current?.cancel();
    };
  }, [invalidateRequest]);

  return {
    toolEvents,
    supervisorRun,
    messages,
    isLoading,
    inputText,
    currentLoadingMessage,
    setInputText,
    mapData,
    centerLocation,
    selectAnswer,
    searchArea: centerLocation?.name?.trim() || null,
    handleSubmit,
    canResume,
    resumeAccepted,
  };
}
