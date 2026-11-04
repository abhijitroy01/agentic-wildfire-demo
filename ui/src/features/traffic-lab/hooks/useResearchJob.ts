import type { Network,Result } from "@/features/evacuation/artifacts";
/**
 * Traffic Lab run lifecycle: workspace access, run history, the selected run, polling, cancellation and
 * loading a completed run's artifacts. Each effect owns an AbortController, so a superseded or unmounted
 * lab stops its requests. Polling runs only once the workspace session is ready and sends one request at
 * a time.
 */
import { isActiveStatus } from "@/features/evacuation/domain";
import { useEffect,useRef,useState } from "react";
import { newerStatus, readDelay, temporaryRead } from "@/features/evacuation/services/readRecovery";
import { BrokerError } from "@/shared/services/workspaceAccess";
import { initialize,research,type ResearchConfig,type ResearchJob } from "../services/api";
import { acknowledge, forgetIntent, readIntent, retainedIntent } from "../services/requestIntent";

/** Workspace state: still connecting (or failed to connect; see `error`), or ready. */
export type Access = "connecting" | "ready" | "failed";

const RUN_PARAM = "traffic_run";
const POLL_INTERVAL_MS = 2_500;
// A status read is small: give up well before the artifact timeout and retry on the next poll.
const POLL_TIMEOUT_MS = 15_000;

/** Keep the newer of two snapshots of one run, so a late "running" poll cannot undo "cancelling". */
export function newerJob(previous: ResearchJob | null, next: ResearchJob): ResearchJob {
  return previous?.id === next.id ? newerStatus(previous, next) : next;
}

/** The completed run named by ?traffic_run=, if any. */
export const savedRunId = (search: string): string | null => new URLSearchParams(search).get(RUN_PARAM);

/** Point ?traffic_run= at the displayed run, or remove it, without adding a history entry. */
function setRunParam(id: string | null): void {
  const url = new URL(location.href);
  if (id) url.searchParams.set(RUN_PARAM, id); else url.searchParams.delete(RUN_PARAM);
  if (url.href !== location.href) window.history.replaceState(window.history.state, "", url);
}

const message = (error: unknown): string => error instanceof Error ? error.message : String(error);

type Options = {
  /** The lab is on screen. Only a visible lab rewrites ?traffic_run=. */
  readonly visible: boolean;
  /** Load the starting example when no saved run is named in the URL. */
  readonly loadStart: () => Promise<void>;
  /** A completed run's artifacts arrived; reset the replay to show them. */
  readonly onLoaded: (result: Result, job: ResearchJob) => void;
};

export type ResearchJobState = {
  readonly access: Access;
  readonly config: ResearchConfig | null;
  /** The selected run: started here, chosen from history, or restored from ?traffic_run=. */
  readonly job: ResearchJob | null;
  readonly history: readonly ResearchJob[];
  /** Saved artifacts of the selected completed run. */
  readonly result: Result | null;
  readonly network: Network | null;
  /** The selected run can still change (queued, running or cancelling). */
  readonly running: boolean;
  /** The selected run completed and its artifacts are downloading. */
  readonly loading: boolean;
  /** The selected run completed but its artifacts could not be loaded; see `error`. */
  readonly loadFailed: boolean;
  /** The run remains active on the server but a permanent status-read failure stopped polling. */
  readonly readStopped: boolean;
  readonly error: string;
  readonly setError: (message: string) => void;
  /** Start a three-policy run for a scenario. */
  readonly start: (scenario: object) => Promise<void>;
  /** Select a run from history; a completed run's artifacts load automatically. */
  readonly select: (id: string) => Promise<void>;
  /** Ask the server to cancel the selected run. */
  readonly cancel: () => Promise<void>;
  /** Retry loading the selected completed run's artifacts. */
  readonly reload: () => void;
  /** Forget the selected run and its replay, for example after the study area changes. */
  readonly clear: () => void;
};

/** React hook for Traffic Lab access, config, the current run, its polling and run history. */
export function useResearchJob({ visible, loadStart, onLoaded }: Options): ResearchJobState {
  const [access, setAccess] = useState<Access>("connecting");
  const [config, setConfig] = useState<ResearchConfig | null>(null);
  const [job, setJob] = useState<ResearchJob | null>(null);
  const [history, setHistory] = useState<readonly ResearchJob[]>([]);
  const [artifacts, setArtifacts] = useState<{ id: string; result: Result; network: Network } | null>(null);
  const [failedId, setFailedId] = useState<string | null>(null);
  const [readStopped, setReadStopped] = useState(false);
  // undefined until the lab first decides which run it shows; the address bar is left alone until then.
  const [urlRun, setUrlRun] = useState<string | null | undefined>(undefined);
  const [error, setError] = useState("");
  const [attempt, setAttempt] = useState(0);
  const [accessAttempt, setAccessAttempt] = useState(0);
  const latest = useRef({ job, loadedId: artifacts?.id, loadStart, onLoaded, visible });
  latest.current = { job, loadedId: artifacts?.id, loadStart, onLoaded, visible };
  const selection = useRef(0);
  const unavailableSavedRun = useRef<string | null>(null);
  const jobId = job?.id ?? null;
  const ready = access === "ready";

  // Workspace access, server config and history, then the saved run or the starting example.
  useEffect(() => {
    const controller = new AbortController();
    const { signal } = controller;
    setAccess("connecting"); setError("");
    (async (): Promise<void> => {
      await initialize(signal);
      if (signal.aborted) return;
      const retained = savedRunId(location.search) || readIntent("research")?.jobId;
      const saved = retained === unavailableSavedRun.current ? null : retained;
      const settings = await research<ResearchConfig>("/config", undefined, { signal });
      if (signal.aborted) return;
      setConfig(settings);
      const jobs = await research<ResearchJob[]>("/jobs", undefined, { signal });
      if (signal.aborted) return;
      setHistory(jobs);
      if (!saved) { await latest.current.loadStart(); if (!signal.aborted) setAccess("ready"); return; }
      let selected: ResearchJob;
      try {
        selected = await research<ResearchJob>(`/jobs/${encodeURIComponent(saved)}`, undefined, { signal });
      } catch (e) {
        if (!signal.aborted && e instanceof BrokerError && [404,410].includes(e.httpStatus)) {
          unavailableSavedRun.current = saved;
          if (readIntent("research")?.jobId === saved) forgetIntent("research");
          setUrlRun(null);
          if (latest.current.visible && savedRunId(location.search) === saved) setRunParam(null);
          throw new Error("The saved research run is no longer available. Reconnect to choose a new scenario.");
        }
        throw e;
      }
      if (signal.aborted) return;
      setAccess("ready");
      setJob(selected); // The run effect below loads its artifacts.
    })().catch((e: unknown) => { if (!signal.aborted) { setAccess("failed"); setError(message(e)); } });
    return () => controller.abort();
  }, [accessAttempt]);

  // Poll the selected run while it is active, then load a completed run's artifacts. Keyed on the run
  // id (not its status), so the completion that ends polling cannot cancel the artifact download.
  useEffect(() => {
    if (!jobId || !ready) return;
    const id = jobId;
    const controller = new AbortController();
    const { signal } = controller;
    let timer: number | undefined;
    let pollError = "";
    let readFailures = 0;
    const later = (delay = POLL_INTERVAL_MS): void => {
      if (!signal.aborted) timer = window.setTimeout(() => void poll(), delay);
    };
    const poll = async (): Promise<void> => {
      let current = latest.current.job;
      if (current?.id !== id) return;
      let finished = false;
      if (isActiveStatus(current.status)) {
        try {
          const polled = await research<ResearchJob>(`/jobs/${id}`, undefined, { signal, timeoutMs: POLL_TIMEOUT_MS });
          if (signal.aborted) return;
          current = newerJob(latest.current.job, polled);
          latest.current.job = current;
          setJob(previous => (previous?.id === id ? newerJob(previous, polled) : previous));
          setReadStopped(false);
          if (pollError) {
            const stale = pollError;
            pollError = "";
            setError(shown => (shown === stale ? "" : shown));
          }
          finished = !isActiveStatus(current.status);
        } catch (e) {
          if (signal.aborted) return;
          pollError = message(e);
          setError(pollError);
          if (temporaryRead(e)) later(readDelay(e)); else setReadStopped(true);
          return;
        }
        if (!finished) { later(); return; }
      }
      let downloading = false;
      try {
        if (current.status === "failed") setError(current.error || "Run failed");
        if (current.status === "completed" && latest.current.loadedId !== id) {
          downloading = true;
          const [result, network] = await Promise.all([
            research<Result>(`/jobs/${id}/artifacts/result`, undefined, { signal }),
            research<Network>(`/jobs/${id}/artifacts/network`, undefined, { signal }),
          ]);
          if (signal.aborted) return;
          downloading = false;
          setArtifacts({ id, result, network }); setFailedId(null); setUrlRun(id);
          setError("");
          latest.current.onLoaded(result, current);
        }
        if (finished) {
          const jobs = await research<ResearchJob[]>("/jobs", undefined, { signal });
          if (!signal.aborted) setHistory(jobs);
        }
      } catch (e) {
        if (signal.aborted) return;
        if (downloading && temporaryRead(e) && ++readFailures < 3) {
          setError(message(e)); later(readDelay(e, readFailures - 1)); return;
        }
        if (downloading) setFailedId(id);
        setError(message(e));
      }
    };
    void poll();
    return () => { controller.abort(); window.clearTimeout(timer); };
  }, [jobId, ready, attempt]);

  // Only the visible lab may rewrite the address bar; becoming visible again re-syncs it.
  useEffect(() => {
    if (visible && ready && urlRun !== undefined) setRunParam(urlRun);
  }, [visible, ready, urlRun]);

  const start = async (scenario: object): Promise<void> => {
    const intent = retainedIntent("research", scenario);
    const created = await research<ResearchJob>("/jobs", {...scenario, request_id: intent.requestId});
    acknowledge("research", intent, created.id);
    selection.current += 1;
    setArtifacts(null); setFailedId(null); setReadStopped(false); setUrlRun(null); setJob(created);
    setHistory(await research<ResearchJob[]>("/jobs"));
  };
  const select = async (id: string): Promise<void> => {
    const token = ++selection.current;
    const selected = await research<ResearchJob>(`/jobs/${encodeURIComponent(id)}`);
    if (token !== selection.current) return;
    setError(""); setArtifacts(null); setFailedId(null); setReadStopped(false); setJob(selected);
    // A completed run replaces ?traffic_run= once its artifacts load; any other run has no replay to link.
    if (selected.status !== "completed") setUrlRun(null);
  };
  const cancel = async (): Promise<void> => {
    const id = latest.current.job?.id;
    if (!id) return;
    const next = await research<ResearchJob>(`/jobs/${encodeURIComponent(id)}/cancel`, {});
    setJob(previous => (previous?.id === id ? newerJob(previous, next) : previous));
    if (readStopped) { setReadStopped(false); setAttempt(n => n + 1); }
  };
  const reload = (): void => {
    if (access === "failed") setAccessAttempt(n => n + 1);
    else { setFailedId(null); setReadStopped(false); setAttempt(n => n + 1); }
  };
  const clear = (): void => {
    selection.current += 1;
    setArtifacts(null); setFailedId(null); setReadStopped(false); setUrlRun(null); setJob(null);
  };

  return {
    access, config, job, history, error: error || (job?.status === "failed" ? job.error || "Run failed" : ""),
    setError, start, select, cancel, reload, clear, readStopped,
    result: artifacts?.result ?? null,
    network: artifacts?.network ?? null,
    running: !!job && isActiveStatus(job.status),
    loading: job?.status === "completed" && artifacts?.id !== job.id && failedId !== job.id,
    loadFailed: !!job && failedId === job.id,
  };
}
