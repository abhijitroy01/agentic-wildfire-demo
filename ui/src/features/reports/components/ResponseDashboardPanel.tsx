/**
 * ResponseDashboardPanel - Real-time firetruck response tracking panel
 * Displays the private firetruck demonstration, with durable evidence-backed reports.
 *
 * MIGRATED: Now uses DockedPanel primitive for positioning, animation, z-index, ESC, ARIA.
 * All firetruck-specific logic (WebSocket, dispatch, truck state) stays here.
 */
import TruckCard from "@/features/firetruck/components/TruckCard";
import {
checkHealth,
generateFiretruckReport,getFiretruckReport,
getSimulationStatus,getSimulationRequest,getFiretruckReportRequest,
startSimulation,
stopSimulation,
type FiretruckReport,
type SimulationConfig,
} from "@/features/firetruck/services/firetruckService";
import { formatElapsedTime,type TruckView } from "@/features/firetruck/utils/truckDisplayUtils";
import { useResponseRegistryContext } from "@/features/reports/ResponseRegistryContext";
import WorkspacePanel from "@/shared/components/WorkspacePanel";
import { logger } from "@/shared/utils/logger";
import { BrokerError,isTransientReadError,BROKER_PATH } from "@/shared/services/workspaceAccess";
import { tabStorage } from "@/shared/services/tabStorage";
import type React from "react";
import { memo,useCallback,useEffect,useRef,useState } from "react";
import "./ResponseDashboardPanel.css";

// --- Type Definitions ---

interface FireData {
  fireId: string;
  fireName: string;
  latitude: number;
  longitude: number;
  acres?: number;
}

interface ApiTruck {
  truck_id: string;
  unit_type: string;
  latitude: number;
  longitude: number;
  heading: number;
  speed_mph: number;
  activity_status: string;
  distance_to_fire_ft: number;
  water_tank_level_gal: number;
  water_tank_level_pct: number;
  foam_tank_level_gal: number;
  pump_status: string;
  flow_rate_gpm: number;
  estimated_water_remaining_min: number;
  crew_count: number;
  hours_on_duty: number;
  crew_fatigue_level: string;
  in_danger_zone: boolean;
  entrapment_risk: string;
  escape_route_clear: boolean;
}

interface ActiveSession {
  session_id: string;
  websocket_url: string;
  fireId: string;
  fireName: string;
}
interface DispatchIntent {
  requestId: string;
  config: SimulationConfig;
  fireId: string;
  fireName: string;
}
interface ReportIntent {requestId: string; runId: string; question: string}
const DISPATCH_KEY = "fireguard_firetruck_dispatch";
const REPORT_REQUEST_KEY = "fireguard_firetruck_report_request";
function missing(error: unknown): boolean {
  return error instanceof BrokerError && [404, 410].includes(error.httpStatus);
}
/** Invalid local records can be discarded; a failed owned-service read must retain its handle. */
function savedRecord<T>(key: string, valid: (value: T) => boolean): T | null {
  const raw = tabStorage.getItem(key);
  if (!raw) return null;
  try {
    const value = JSON.parse(raw) as T;
    if (!value || !valid(value)) throw new Error("Invalid saved record");
    return value;
  } catch {
    logger.warn("Discarded an invalid saved firetruck record.");
    tabStorage.removeItem(key);
    return null;
  }
}

interface WebSocketMessage {
  event_type: string;
  session_id?: string;
  incident_elapsed_seconds?: number;
  trucks?: ApiTruck[];
  reason?: string;
  phase?: string;
  status?: string;
}


function transformTrucks(apiTrucks: ApiTruck[]): TruckView[] {
  return apiTrucks.map((t) => ({
              id: t.truck_id,
              unitType: t.unit_type,
              latitude: t.latitude,
              longitude: t.longitude,
              heading: t.heading,
              speedMph: t.speed_mph,
              status: t.activity_status,
              waterLevelPct: t.water_tank_level_pct,
              pumpActive: t.pump_status === "ACTIVE",
              flowRateGpm: t.flow_rate_gpm,
              crewCount: t.crew_count,
              hoursOnDuty: t.hours_on_duty,
              inDangerZone: t.in_danger_zone,
              entrapmentRisk: t.entrapment_risk,
              position: {
                latitude: t.latitude,
                longitude: t.longitude,
              },
            }));
}

type ApiStatus = "checking" | "online" | "offline";

interface ResponseDashboardPanelProps {
  isOpen: boolean;
  /** Whether 3 · Response Tracker is the current stage; the exercise runs only there. */
  inResponseTracker: boolean;
  onClose: () => void;
  /** The exercise map, centered on the selected fire and showing the exercise's units. */
  renderMap?: (center: {latitude: number; longitude: number; name?: string} | null, trucks: TruckView[]) => React.ReactNode;
}

const ResponseDashboardPanel: React.FC<ResponseDashboardPanelProps> = memo(
  ({ isOpen, inResponseTracker, onClose, renderMap }) => {
    const [trucks, setTrucks] = useState<TruckView[]>([]);
    const [selectedFireId, setSelectedFireId] = useState<string>("");
    const [truckCount, setTruckCount] = useState<number>(4);
    const [apiStatus, setApiStatus] = useState<ApiStatus>("checking");
    const [activeSession, setActiveSession] = useState<ActiveSession | null>(
      null
    );
    const [elapsedSeconds, setElapsedSeconds] = useState<number>(0);
    const [lastSession, setLastSession] = useState<ActiveSession | null>(null);
    const [phase, setPhase] = useState("");
    const [runStatus, setRunStatus] = useState("");
    const [reportQuestion, setReportQuestion] = useState("Explain the recorded suppression stages, water use, resupply and crew rotation.");
    const [report, setReport] = useState<FiretruckReport | null>(null);
    const [reportBusy, setReportBusy] = useState(false);
    const [error, setError] = useState<string | null>(null);
    const [readError, setReadError] = useState<string | null>(null);
    const [readRetry, setReadRetry] = useState(0);
    const [healthRetry, setHealthRetry] = useState(0);
    const [dispatchBusy, setDispatchBusy] = useState(false);
    const [pendingDispatch, setPendingDispatch] = useState<DispatchIntent | null>(null);
    const [retryDispatch, setRetryDispatch] = useState(false);
    const [pendingReportRequest, setPendingReportRequest] = useState(false);
    const [unresolvedRun, setUnresolvedRun] = useState(() => !!tabStorage.getItem("fireguard_firetruck_session"));
    const [unresolvedReport, setUnresolvedReport] = useState(() => !!(
      tabStorage.getItem("fireguard_firetruck_report") || tabStorage.getItem(REPORT_REQUEST_KEY)));
    const dispatchFlight = useRef(false);
    const dispatchIntent = useRef<DispatchIntent | null>(null);
    const reportIntent = useRef<ReportIntent | null>(null);
    const reportFlight = useRef(false);

    // Get fires from response registry
    const { getKnownFires } = useResponseRegistryContext();

    // Get fires from registry
    const fires = getKnownFires();

    // Refs
    const wsRef = useRef<WebSocket | null>(null);
    // The run whose stop request was sent, so Stop and a stage change send at most one request per run.
    const stopRequested = useRef<ActiveSession | null>(null);

    const receiveSession = useCallback((session: ActiveSession, status: string): void => {
      setLastSession(session);
      setUnresolvedRun(false);
      setRunStatus(status);
      setActiveSession(["starting", "running"].includes(status) ? session : null);
      tabStorage.setItem("fireguard_firetruck_session", JSON.stringify(session));
    }, []);
    const receiveDispatch = useCallback((intent: DispatchIntent, sessionId: string, status: string): void => {
      receiveSession({session_id: sessionId, fireId: intent.fireId, fireName: intent.fireName,
        websocket_url: `${location.origin.replace(/^http/, "ws")}${BROKER_PATH}/firetruck/runs/${encodeURIComponent(sessionId)}/stream`},
      status);
      dispatchIntent.current = null;
      setPendingDispatch(null);
      setRetryDispatch(false);
      tabStorage.removeItem(DISPATCH_KEY);
      setReport(null);
      tabStorage.removeItem("fireguard_firetruck_report");
      tabStorage.removeItem(REPORT_REQUEST_KEY);
      reportIntent.current = null;
      setPendingReportRequest(false);
      setUnresolvedReport(false);
    }, [receiveSession]);

    // Recheck on opening and explicitly after an outage; ignore a retired health response.
    useEffect(() => {
      let current = true;
      const controller = new AbortController();
      const checkApiHealth = async (): Promise<void> => {
        setApiStatus("checking");
        const isHealthy = await checkHealth(controller.signal);
        if (!current) return;
        if (isHealthy) {
          setApiStatus("online");
          logger.log("✅ Firetruck simulator API is online");
        } else {
          setApiStatus("offline");
        }
      };

      void checkApiHealth();
      return () => { current = false; controller.abort(); };
    }, [isOpen, healthRetry]);

    useEffect(() => {
      let cancelled = false;
      const controller = new AbortController();
      const failedRead = (err: unknown, key?: string): void => {
        if (cancelled || controller.signal.aborted) return;
        if (key && missing(err)) {
          tabStorage.removeItem(key);
          if (key === "fireguard_firetruck_session") setUnresolvedRun(false);
          if (key === "fireguard_firetruck_report") setUnresolvedReport(false);
        }
        logger.error("Saved firetruck evidence could not be read:", err);
        setReadError(err instanceof Error ? err.message : "Saved simulation evidence is unavailable.");
      };
      setReadError(null);
      void (async () => {
        const intent = savedRecord<DispatchIntent>(DISPATCH_KEY, value =>
          typeof value.requestId === "string" && !!value.config?.fire_location
          && typeof value.fireId === "string" && typeof value.fireName === "string");
        if (intent) {
          dispatchIntent.current = intent;
          setPendingDispatch(intent);
          try {
            const receipt = await getSimulationRequest(intent.requestId, controller.signal);
            if (!cancelled) receiveDispatch(intent, receipt.session_id, receipt.status);
          } catch (err) {
            if (!cancelled && missing(err)) setRetryDispatch(true);
            failedRead(err);
          }
        }
        if (!cancelled) {
          const session = savedRecord<ActiveSession>("fireguard_firetruck_session", value =>
            typeof value.session_id === "string" && typeof value.fireId === "string"
            && typeof value.fireName === "string");
          if (session) {
            try {
              const result = await getSimulationStatus(session.session_id, controller.signal);
              if (!cancelled) {
                session.websocket_url = `${location.origin.replace(/^http/, "ws")}${BROKER_PATH}/firetruck/runs/${encodeURIComponent(session.session_id)}/stream`;
                receiveSession(session, result.status || "unavailable");
                setPhase(result.phase || "");
                setElapsedSeconds(result.incident_elapsed_seconds ?? 0);
                setTrucks(transformTrucks((result.trucks || []) as ApiTruck[]));
              }
            } catch (err) { failedRead(err, "fireguard_firetruck_session"); }
          } else setUnresolvedRun(false);
        }
        if (cancelled) return;
        const reportRequest = savedRecord<ReportIntent>(REPORT_REQUEST_KEY, value =>
          typeof value.runId === "string" && typeof value.requestId === "string"
          && typeof value.question === "string");
        if (reportRequest) {
          reportIntent.current = reportRequest;
          setPendingReportRequest(true);
          try {
            const receipt = await getFiretruckReportRequest(reportRequest.runId, reportRequest.requestId, controller.signal);
            if (!cancelled) {
              setReport(receipt);
              setUnresolvedReport(false);
              tabStorage.setItem("fireguard_firetruck_report", receipt.report_id);
              tabStorage.removeItem(REPORT_REQUEST_KEY);
              reportIntent.current = null;
              setPendingReportRequest(false);
            }
          } catch (err) {
            if (!cancelled && missing(err)) setUnresolvedReport(false);
            failedRead(err);
          }
        } else {
          const savedReport = tabStorage.getItem("fireguard_firetruck_report");
          if (savedReport) {
            try {
              const value = await getFiretruckReport(savedReport, controller.signal);
              if (!cancelled) { setReport(value); setUnresolvedReport(false); }
            } catch (err) { failedRead(err, "fireguard_firetruck_report"); }
          } else setUnresolvedReport(false);
        }
      })();
      return () => { cancelled = true; controller.abort(); };
    }, [readRetry, receiveDispatch, receiveSession]);

    useEffect(() => {
      if (report?.status !== "running") return;
      let cancelled = false;
      const controller = new AbortController();
      let timer: ReturnType<typeof setTimeout> | undefined;
      let failures = 0;
      const poll = async (): Promise<void> => {
        let again = true;
        try {
          const value = await getFiretruckReport(report.report_id, controller.signal);
          if (cancelled) return;
          failures = 0;
          setReadError(null);
          setReport(value);
          again = value.status === "running";
        } catch (err) {
          if (cancelled || controller.signal.aborted) return;
          logger.error("Firetruck report status read failed:", err);
          setReadError(err instanceof Error ? err.message : "Report unavailable");
          again = isTransientReadError(err) && ++failures < 5;
        }
        if (!cancelled && again) timer = setTimeout(() => void poll(), 2500);
      };
      timer = setTimeout(() => void poll(), 2500);
      return () => { cancelled = true; controller.abort(); clearTimeout(timer); };
    }, [report?.report_id, report?.status, readRetry]);

    // The stream is read-only; start/stop use owner-checked HTTP requests.
    useEffect(() => {
      if (!activeSession?.websocket_url) return;
      let cancelled = false;
      let reconnect: number | undefined;
      const connect = (): void => {
        if (cancelled) return;
        const ws = new WebSocket(activeSession.websocket_url);
        wsRef.current = ws;
        ws.onopen = () => { if (!cancelled) setError(null); };
      ws.onmessage = (event: MessageEvent): void => {
        if (cancelled) return;
        try {
          const message: WebSocketMessage = JSON.parse(event.data);

          if (message.event_type === "CONNECTED") {
            logger.log("📡 Connected to simulation:", message.session_id);
          } else if (message.event_type === "TRUCK_UPDATE") {
            setElapsedSeconds(message.incident_elapsed_seconds ?? 0);
            setPhase(message.phase || "");
            setRunStatus(message.status || "running");

            setTrucks(transformTrucks(message.trucks || []));
          } else if (message.event_type === "SIMULATION_STOPPED") {
            logger.log("⏹️ Simulation stopped:", message.reason);
            setActiveSession(null);
            setRunStatus(message.status || "stopped");
          }
        } catch (err) {
          logger.error("Failed to parse WebSocket message:", err);
        }
      };

        ws.onerror = () => { if (!cancelled) setError("Truck updates disconnected; reconnecting…"); };
        ws.onclose = event => {
          if (!cancelled && event.code !== 1008) reconnect = window.setTimeout(connect, 3000);
          if (!cancelled && event.code === 1008) setError("Session expired. Reload the workspace.");
        };
      };
      connect();
      return () => { cancelled = true; window.clearTimeout(reconnect); wsRef.current?.close(); };
    }, [activeSession]);

    // Handle dispatch
    const handleDispatch = useCallback(async (): Promise<void> => {
      if (dispatchFlight.current || reportFlight.current || activeSession || unresolvedRun
        || unresolvedReport || pendingReportRequest || apiStatus !== "online") return;

      const selectedFire = fires.find((f) => f.fireId === selectedFireId);
      if (!selectedFire && !dispatchIntent.current) return;
      if (dispatchIntent.current && !retryDispatch) return;

      setError(null);
      dispatchFlight.current = true;
      setDispatchBusy(true);
      const intent = dispatchIntent.current ?? {
        requestId: crypto.randomUUID(), fireId: selectedFire!.fireId, fireName: selectedFire!.fireName,
        config: {fire_location: {latitude: selectedFire!.location.lat, longitude: selectedFire!.location.lng,
          name: selectedFire!.fireName}, initial_truck_count: truckCount, max_truck_count: 12},
      };
      dispatchIntent.current = intent;
      setPendingDispatch(intent);
      tabStorage.setItem(DISPATCH_KEY, JSON.stringify(intent));

      try {
        const result = await startSimulation(intent.config, intent.requestId);
        receiveDispatch(intent, result.session_id, result.status || "starting");
        logger.log("🚒 Simulation started:", result.session_id);
      } catch (err) {
        logger.error("Failed to start simulation:", err);
        setError("The dispatch response was not confirmed. Checking the same request before another dispatch.");
        try {
          const receipt = await getSimulationRequest(intent.requestId);
          receiveDispatch(intent, receipt.session_id, receipt.status);
          setError(null);
        } catch (readErr) {
          setRetryDispatch(missing(readErr));
          setReadError(readErr instanceof Error ? readErr.message : "Dispatch status unavailable.");
        }
      } finally {
        dispatchFlight.current = false;
        setDispatchBusy(false);
      }

      setSelectedFireId("");
    }, [selectedFireId, fires, truckCount, apiStatus, activeSession, unresolvedRun, unresolvedReport,
      pendingReportRequest, retryDispatch, receiveDispatch]);

    /** Send a run's stop request once; a repeated Stop or a later stage change sends nothing more for that run. */
    const stopRun = useCallback(async (session: ActiveSession): Promise<void> => {
      if (stopRequested.current === session) return;
      stopRequested.current = session;

      try {
        await stopSimulation(session.session_id);
        // Wait for the service's final snapshot before claiming stopped.
        setRunStatus(previous => ["stopped", "interrupted"].includes(previous) ? previous : "stopping");
      } catch (err) {
        // Only the Stop button can send it again. After a failed stop on leaving the stage, the run goes on until a
        // reload restores it (with its Stop button, or stopped again outside the stage) or its lifetime ends.
        stopRequested.current = null;
        logger.error("Failed to stop simulation:", err);
        const errorMessage =
          err instanceof Error ? err.message : "Failed to stop simulation";
        setError(errorMessage);
      }
    }, []);

    const handleStop = useCallback((): void => {
      if (activeSession) void stopRun(activeSession);
    }, [activeSession, stopRun]);

    // The exercise runs only in 3 · Response Tracker. Leaving the stage stops the active run, closes its stream
    // (the stream effect's cleanup) and clears its units. The stopped run stays the last session for its report,
    // and the next run needs an explicit Dispatch.
    useEffect(() => {
      if (inResponseTracker) return;
      if (activeSession) void stopRun(activeSession);
      setActiveSession(null);
      setTrucks(previous => (previous.length ? [] : previous));
    }, [inResponseTracker, activeSession, stopRun]);

    // A run stopped by leaving reads "stopping": its closed stream never brings the final snapshot. When the exercise
    // shows again, read the run once and apply only its status and phase, so its units stay cleared and nothing
    // resumes. While the service still reports the run as live, "stopping" stays.
    useEffect(() => {
      if (!isOpen || activeSession || !lastSession || runStatus !== "stopping") return;
      let cancelled = false;
      getSimulationStatus(lastSession.session_id).then(result => {
        if (cancelled || result.active) return;
        setRunStatus(result.status || "unavailable");
        setPhase(result.phase || "");
      }).catch(err => logger.warn("⚠️ Could not read the stopped run's status:", err));
      return () => { cancelled = true; };
    }, [isOpen, activeSession, lastSession, runStatus]);

    const handleReport = async (): Promise<void> => {
      if (!lastSession || dispatchFlight.current || reportFlight.current || unresolvedReport) return;
      reportFlight.current = true;
      setReportBusy(true);
      setError(null);
      const intent = reportIntent.current ?? {requestId: crypto.randomUUID(), runId: lastSession.session_id,
        question: reportQuestion};
      reportIntent.current = intent;
      setPendingReportRequest(true);
      tabStorage.setItem(REPORT_REQUEST_KEY, JSON.stringify(intent));
      try {
        const created = await generateFiretruckReport(intent.runId, intent.question, intent.requestId);
        setReport(created);
        setUnresolvedReport(false);
        tabStorage.setItem("fireguard_firetruck_report", created.report_id);
        tabStorage.removeItem(REPORT_REQUEST_KEY);
        reportIntent.current = null;
        setPendingReportRequest(false);
      } catch (err) {
        logger.error("Firetruck report response was not confirmed:", err);
        try {
          const receipt = await getFiretruckReportRequest(intent.runId, intent.requestId);
          setReport(receipt);
          setUnresolvedReport(false);
          tabStorage.setItem("fireguard_firetruck_report", receipt.report_id);
          tabStorage.removeItem(REPORT_REQUEST_KEY);
          reportIntent.current = null;
          setPendingReportRequest(false);
        } catch (readErr) {
          setUnresolvedReport(!missing(readErr));
          setReadError(readErr instanceof Error ? readErr.message : "Report request status unavailable.");
        }
      } finally { reportFlight.current = false; setReportBusy(false); }
    };

    const selectedKnownFire = fires.find(value =>
      value.fireId === (activeSession?.fireId || selectedFireId || lastSession?.fireId));
    const selectedMapCenter = selectedKnownFire ? {latitude:selectedKnownFire.location.lat,
      longitude:selectedKnownFire.location.lng, name:selectedKnownFire.fireName} : null;
    return (
      <WorkspacePanel
        id="response-dashboard"
        isOpen={isOpen}
        onClose={onClose}
        title="Standalone firetruck exercise"
        icon="🚒"
        width={380}
        className="response-dashboard-wrapper"
      >
        <p className="rdp-demo-note">Synthetic fire suppression exercise. Crew staging in the evacuation simulation is a separate outcome.</p>
        <aside className="response-workspace-controls">
        {/* API Status */}
        <div className="rdp-api-status" role="status" aria-live="polite">
          <span className={`rdp-status-dot ${apiStatus}`} aria-hidden="true" />
          <span className="rdp-status-text">
            {apiStatus === "online"
              ? "Connected"
              : apiStatus === "checking"
                ? "Checking connection..."
                : "Offline"}
          </span>
        </div>
        {apiStatus === "offline" && <button type="button" onClick={() => setHealthRetry(value => value + 1)}>
          Recheck firetruck service
        </button>}

        {/* Error Display */}
        {error && (
          <div className="rdp-error" role="alert" aria-live="assertive">
            <span aria-hidden="true">⚠️</span>
            <span>{error}</span>
          </div>
        )}
        {readError && <div className="rdp-error" role="alert">
          <span>{readError}</span>
          <button type="button" onClick={() => setReadRetry(value => value + 1)}>Retry saved reads</button>
        </div>}
        {pendingDispatch && <p role="status">{dispatchBusy ? "Dispatching…" : retryDispatch
          ? "No accepted dispatch was found. Retry uses the same request."
          : "Dispatch outcome is being reconciled; another exercise will not be started."}</p>}
        {pendingDispatch && retryDispatch && <button type="button" disabled={dispatchBusy}
          onClick={() => void handleDispatch()}>Retry same dispatch</button>}
        {unresolvedRun && <p role="status">Reading the retained exercise before another dispatch is allowed.</p>}

        {lastSession && <section className="rdp-report" aria-label="Simulation report">
          <p>{lastSession.fireName} · {runStatus} {phase && `· ${phase.replace(/_/g, " ")}`}</p>
          {(reportBusy || report?.status === "running") && <p role="status">Report agent is interpreting the saved suppression evidence.</p>}
          <label htmlFor="firetruck-report-question">Ask about this simulation</label>
          <textarea id="firetruck-report-question" value={reportQuestion} maxLength={2000}
            onChange={event => setReportQuestion(event.target.value)} />
          <button onClick={handleReport} disabled={unresolvedReport || reportBusy || report?.status === "running" || !reportQuestion.trim()}>
            {reportBusy || report?.status === "running" ? "Generating report…" : pendingReportRequest
              ? "Retry same report request" : "Generate report"}
          </button>
          {unresolvedReport && <p role="status">Reading the retained report request before another report is allowed.</p>}
          {report?.status === "completed" && <a target="_blank" rel="noopener noreferrer"
            href={`/simulation-api/v1/firetruck/reports/${encodeURIComponent(report.report_id)}/html`}>Open report</a>}
          {report && ["failed", "interrupted"].includes(report.status) && <p role="status">Report {report.status}. Saved simulation evidence remains available; try again.</p>}
        </section>}

        {/* Dispatch Controls */}
        {apiStatus === "online" && !activeSession && !pendingDispatch && !unresolvedRun
          && !unresolvedReport && !pendingReportRequest && (
          <div className="rdp-dispatch-section" role="region" aria-label="Dispatch controls">
            <h4 id="dispatch-heading">Start suppression exercise</h4>
            {fires.length === 0 && <p>Run a radio investigation or Simulation Lab scenario to add a resolved location.</p>}
            <div className="rdp-dispatch-controls" aria-labelledby="dispatch-heading">
              <label htmlFor="fire-select" className="visually-hidden">
                Select fire incident
              </label>
              <select
                id="fire-select"
                value={selectedFireId}
                onChange={(e) => setSelectedFireId(e.target.value)}
                className="rdp-fire-select"
                aria-required="true"
              >
                <option value="">Select Fire...</option>
                {fires.map((fire) => (
                  <option key={fire.fireId} value={fire.fireId}>
                    {fire.fireName}
                  </option>
                ))}
              </select>
              <div className="rdp-truck-count">
                <label htmlFor="truck-count">Trucks:</label>
                <input
                  id="truck-count"
                  type="number"
                  min="2"
                  max="12"
                  value={truckCount}
                  onChange={(e) => setTruckCount(parseInt(e.target.value) || 4)}
                  aria-describedby="truck-count-help"
                />
                <span id="truck-count-help" className="visually-hidden">
                  Number of trucks to dispatch, between 2 and 12
                </span>
              </div>
              <button
                className="rdp-dispatch-btn"
                onClick={handleDispatch}
                disabled={!selectedFireId || dispatchBusy}
                aria-disabled={!selectedFireId || dispatchBusy}
                aria-label={selectedFireId ? `Dispatch ${truckCount} trucks` : "Select a fire first"}
              >
                <span aria-hidden="true">🚒</span> Dispatch
              </button>
            </div>
          </div>
        )}

        {/* Active Simulation Header */}
        {activeSession && (
          <div
            className="rdp-active-simulation"
            role="region"
            aria-label="Active simulation"
            aria-live="polite"
          >
            <div className="rdp-sim-header">
              <span className="rdp-sim-fire">
                <span aria-hidden="true">🔥</span> {activeSession.fireName}
              </span>
              <span className="rdp-sim-time" aria-label={`Elapsed time: ${formatElapsedTime(elapsedSeconds)}`}>
                <span aria-hidden="true">⏱️</span> {formatElapsedTime(elapsedSeconds)}
              </span>
              <button
                className="rdp-stop-btn"
                onClick={handleStop}
                aria-label="Stop simulation"
              >
                <span aria-hidden="true">⏹</span> Stop
              </button>
            </div>
          </div>
        )}

        </aside>
        <div className="response-workspace-evidence">
        {isOpen && renderMap && <div className="response-workspace-map">{renderMap(selectedMapCenter, trucks)}</div>}
        {/* Truck List */}
        <div
          className="rdp-responses-section"
          role="region"
          aria-label="Active fire trucks"
          aria-live="polite"
        >
          {trucks.length === 0 ? (
            <div className="rdp-empty-state" role="status">
              <span aria-hidden="true">🚒</span>
              <p>
                {apiStatus !== "online"
                  ? "Connect to simulator to dispatch trucks"
                  : activeSession
                    ? "Waiting for trucks..."
                    : "Select a fire and dispatch trucks"}
              </p>
            </div>
          ) : (
            <ul className="rdp-truck-list" aria-label={`${trucks.length} active trucks`}>
              {trucks.map((truck) => (
                <li key={truck.id}>
                  <TruckCard truck={truck} />
                </li>
              ))}
            </ul>
          )}
        </div>

        {/* Footer with stats */}
        {trucks.length > 0 && (
          <footer className="rdp-footer" role="contentinfo" aria-label="Truck statistics">
            <span aria-label={`${trucks.length} units deployed`}>
              <span aria-hidden="true">🚒</span> {trucks.length} units
            </span>
            <span aria-label={`Average water level: ${Math.round(trucks.reduce((sum, t) => sum + t.waterLevelPct, 0) / trucks.length)}%`}>
              <span aria-hidden="true">💧</span> Avg:{" "}
              {Math.round(
                trucks.reduce((sum, t) => sum + t.waterLevelPct, 0) /
                  trucks.length
              )}
              %
            </span>
            <span aria-label={`${trucks.filter((t) => t.inDangerZone).length} trucks in danger zone`}>
              <span aria-hidden="true">⚠️</span> In Danger: {trucks.filter((t) => t.inDangerZone).length}
            </span>
          </footer>
        )}
        </div>
      </WorkspacePanel>
    );
  }
);

ResponseDashboardPanel.displayName = "ResponseDashboardPanel";

export default ResponseDashboardPanel;

// Export types
export type { FireData,ResponseDashboardPanelProps };
