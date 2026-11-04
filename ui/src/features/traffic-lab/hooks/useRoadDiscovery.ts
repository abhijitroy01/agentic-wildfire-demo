import { useCallback, useEffect, useRef, useState } from "react";
import { isActiveStatus } from "@/features/evacuation/domain";
import type { Discovery } from "@/features/evacuation/artifacts";
import { newerStatus, readDelay, temporaryRead } from "@/features/evacuation/services/readRecovery";
import { discoveryJob, research, type DiscoveryArea, type ResearchDiscoveryJob } from "../services/api";
import { acknowledge, forgetIntent, readIntent, retainedIntent } from "../services/requestIntent";

const POLL_MS = 2500;
const message = (error: unknown): string => error instanceof Error ? error.message : String(error);
const keyFor = (area: DiscoveryArea): string => JSON.stringify([area.lat, area.lon, area.radius_km]);

interface DiscoveryState {
  areaKey: string;
  job: ResearchDiscoveryJob | null;
  starting: boolean;
  error: string;
  readFailed?: boolean;
}

/** Explicit discovery creates one worker job; subsequent bounded GETs have no provider effects or overall deadline. */
export function useRoadDiscovery(area: DiscoveryArea, onCompleted: (result: Discovery) => void): {
  job: ResearchDiscoveryJob | null;
  pending: boolean;
  error: string;
  start: () => Promise<void>;
  cancel: () => Promise<void>;
  clear: () => void;
} {
  const areaKey = keyFor(area);
  const [state, setState] = useState<DiscoveryState>({areaKey, job: null, starting: false, error: ""});
  const latest = useRef({area, areaKey, onCompleted, state});
  latest.current = {area, areaKey, onCompleted, state};
  const active = useRef<{token: number; controller: AbortController; timer?: ReturnType<typeof setTimeout>} | null>(null);
  const revision = useRef(0);

  const clear = useCallback((): void => {
    revision.current += 1;
    active.current?.controller.abort();
    if (active.current?.timer) clearTimeout(active.current.timer);
    active.current = null;
    setState({areaKey: latest.current.areaKey, job: null, starting: false, error: ""});
  }, []);
  useEffect(() => {
    clear();
    return () => {
      revision.current += 1;
      active.current?.controller.abort();
      if (active.current?.timer) clearTimeout(active.current.timer);
      active.current = null;
    };
  }, [areaKey, clear]);

  const same = (token: number, key: string): boolean =>
    revision.current === token && latest.current.areaKey === key && !active.current?.controller.signal.aborted;
  function update(job: ResearchDiscoveryJob, key: string): void {
    setState(previous => previous.areaKey !== key ? previous : {
      areaKey: key, starting: false, error: job.status === "failed" ? job.error || "Road discovery failed." : "",
      job: previous.job?.id === job.id ? newerStatus(previous.job, job) : job,
    });
  }

  async function start(): Promise<void> {
    clear();
    const target = {...latest.current.area}, key = latest.current.areaKey, token = ++revision.current;
    const controller = new AbortController();
    const request = {token, controller, timer: undefined as ReturnType<typeof setTimeout> | undefined};
    active.current = request;
    setState({areaKey: key, job: null, starting: true, error: ""});
    let readFailures = 0;
    const later = (id: string, delay = POLL_MS): void => {
      const job = latest.current.state.job;
      if (same(token, key) && (!job || isActiveStatus(job.status))) {
        request.timer = setTimeout(() => void poll(id), delay);
      }
    };
    const poll = async (id: string): Promise<void> => {
      try {
        const result = discoveryJob(await research<unknown>(`/discover/${encodeURIComponent(id)}`, undefined,
          {signal: controller.signal, timeoutMs: 15000}), target, id);
        if (!same(token, key)) return;
        readFailures = 0;
        update(result, key);
        if (result.status === "completed" && result.result) {
          forgetIntent("discovery"); latest.current.onCompleted(result.result);
        }
        else if (isActiveStatus(result.status)) later(id);
        else forgetIntent("discovery");
      } catch (error) {
        if (!same(token, key)) return;
        const retry = temporaryRead(error) && ++readFailures < 3;
        setState(previous => previous.areaKey === key
          ? {...previous, starting: retry && !previous.job, readFailed: !retry, error: message(error)} : previous);
        if (retry) later(id, readDelay(error, readFailures - 1));
      }
    };
    try {
      const saved = readIntent("discovery");
      if (saved?.body === JSON.stringify(target) && saved.jobId) { void poll(saved.jobId); return; }
      const intent = retainedIntent("discovery", target);
      const created = discoveryJob(await research<unknown>("/discover",
        {...target, request_id: intent.requestId}, {signal: controller.signal}), target);
      acknowledge("discovery", intent, created.id);
      if (!same(token, key)) return;
      update(created, key);
      if (created.status === "completed" && created.result) {
        forgetIntent("discovery"); latest.current.onCompleted(created.result);
      }
      else if (isActiveStatus(created.status)) void poll(created.id);
    } catch (error) {
      if (!same(token, key)) return;
      setState({areaKey: key, job: null, starting: false, error: message(error)});
    }
  }
  async function cancel(): Promise<void> {
    const current = latest.current.state, request = active.current;
    if (!request || current.areaKey !== latest.current.areaKey || !current.job || !isActiveStatus(current.job.status)) return;
    const result = discoveryJob(await research<unknown>(`/discover/${encodeURIComponent(current.job.id)}/cancel`, {},
      {signal: request.controller.signal, timeoutMs: 15000}), latest.current.area, current.job.id);
    if (!same(request.token, current.areaKey)) return;
    update(result, current.areaKey);
    if (result.status === "completed" && result.result) latest.current.onCompleted(result.result);
    if (!isActiveStatus(result.status)) {
      forgetIntent("discovery");
      if (request.timer) clearTimeout(request.timer);
      request.controller.abort();
    }
  }
  const current = state.areaKey === areaKey ? state : {areaKey, job: null, starting: false, error: ""};
  return {job: current.job, pending: current.starting || !current.readFailed && !!current.job && isActiveStatus(current.job.status),
    error: current.error, start, cancel, clear};
}
