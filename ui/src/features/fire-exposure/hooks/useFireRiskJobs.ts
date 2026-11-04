import { useCallback, useEffect, useRef, useState } from "react";
import type { MapData } from "@/features/map/types/types";
import { isTransientReadError } from "@/shared/services/workspaceAccess";
import type { AssessmentResult, FireRiskJobReference, FireRiskJobResponse } from "../generated/contracts";
import { answerAssessments, FireRiskReceiptError } from "../services/assessment";
import { cancelFireRiskJob, readFireRiskJob, retryFireRiskJob } from "../services/api";
import { retainedRetryReferences, savedRetryIntent, saveRetryIntent } from "../services/retryIntent";

const STATUS_POLL_MS = 2000;

export interface FireRiskJobView {
  reference: FireRiskJobReference;
  result: FireRiskJobResponse | null;
  readError: string | null;
  cancelling?: boolean;
  cancelError?: string | null;
  retrying?: boolean;
  retryError?: string | null;
}

interface JobState {
  owner: MapData | null;
  jobs: FireRiskJobView[];
  assessments: AssessmentResult[];
}

/** Poll retained status for this answer only. Selection/unmount aborts GETs and fences late responses. */
export function useFireRiskJobs(mapData: MapData | null): {
  assessments: readonly AssessmentResult[];
  jobs: readonly FireRiskJobView[];
  refreshStatus: () => void;
  cancelJob: (jobId: string) => Promise<void>;
  retryJob: (jobId: string) => Promise<void>;
} {
  const [state, setState] = useState<JobState>({ owner: null, jobs: [], assessments: [] });
  const [revision, setRevision] = useState(0);
  const [retries, setRetries] = useState<{owner: MapData | null; references: FireRiskJobReference[]}>({
    owner: null, references: [],
  });
  const intents = useRef(new Map<string, {requestId: string; pending: boolean}>());
  const reader = useRef<{owner: MapData | null; controller: AbortController} | null>(null);
  const refreshStatus = useCallback((): void => { setRevision(value => value + 1); }, []);

  useEffect(() => {
    const controller = new AbortController();
    reader.current = {owner: mapData, controller};
    let active = true;
    const timers = new Set<ReturnType<typeof setTimeout>>();
    const failedReads = new Map<string, number>();
    const originals = mapData?.fireExposureJobs ?? [];
    const references = [...new Map([...originals, ...retainedRetryReferences(originals),
      ...(retries.owner === mapData ? retries.references : [])].map(item => [item.job_id, item])).values()];
    const saved = mapData?.fireExposureData ?? [];
    const initial: JobState = { owner: mapData, assessments: saved,
      jobs: references.map(reference => {
        const intent = savedRetryIntent(reference);
        return {reference, result: null, readError: null,
          retryError: intent && !intent.reference
            ? "An earlier terrain retry did not return a receipt. Retry to rejoin the same attempt." : null};
      }) };
    setState(previous => previous.owner === mapData ? {
      ...previous, jobs: initial.jobs.map(job =>
        previous.jobs.find(item => item.reference.job_id === job.reference.job_id) ?? job),
    } : initial);
    const later = (reference: FireRiskJobReference): void => {
      if (!active || controller.signal.aborted) return;
      const timer = setTimeout(() => { timers.delete(timer); void poll(reference); }, STATUS_POLL_MS);
      timers.add(timer);
    };

    async function poll(reference: FireRiskJobReference): Promise<void> {
      try {
        const result = await readFireRiskJob(reference, controller.signal);
        if (!active || controller.signal.aborted) return;
        failedReads.delete(reference.job_id);
        const returned = result.assessments ?? [];
        const assessments = answerAssessments(returned, mapData?.locations ?? []);
        if (assessments.length !== returned.length) {
          throw new FireRiskReceiptError("Saved assessment does not belong to the reported fires in this answer.");
        }
        setState(previous => {
          if (previous.owner !== mapData) return previous;
          const jobs = previous.jobs.map(item => item.reference.job_id === reference.job_id
            ? { ...item, reference, result, readError: null } : item);
          if (result.status !== "completed") return { ...previous, jobs };
          const completed = jobs.flatMap(item => item.result?.status === "completed" ? item.result.assessments ?? [] : []);
          return { owner: mapData, jobs,
            assessments: answerAssessments([...saved, ...completed], mapData?.locations ?? []) };
        });
        if (["queued", "running", "cancelling"].includes(result.status)) {
          later(reference);
        }
      } catch (error) {
        if (!active || controller.signal.aborted) return;
        setState(previous => previous.owner !== mapData ? previous : { ...previous,
          jobs: previous.jobs.map(item => item.reference.job_id === reference.job_id
            ? { ...item, readError: error instanceof Error ? error.message : "Saved assessment status unavailable." } : item) });
        // Retained GETs can safely recover from temporary transport delays; invalid identity needs explicit attention.
        const failures = (failedReads.get(reference.job_id) ?? 0)+1;
        failedReads.set(reference.job_id, failures);
        if (!(error instanceof FireRiskReceiptError) && isTransientReadError(error) && failures < 3) later(reference);
      }
    }
    for (const reference of references) void poll(reference);
    return () => {
      active = false;
      controller.abort();
      if (reader.current?.controller === controller) reader.current = null;
      for (const timer of timers) clearTimeout(timer);
    };
  }, [mapData, revision, retries]);

  const cancelJob = useCallback(async (jobId: string): Promise<void> => {
    const current = reader.current;
    const reference = state.owner === mapData
      ? state.jobs.find(item => item.reference.job_id === jobId)?.reference : undefined;
    if (!reference || !current || current.owner !== mapData || current.controller.signal.aborted) return;
    setState(previous => previous.owner !== mapData ? previous : {...previous, jobs: previous.jobs.map(item =>
      item.reference.job_id === jobId ? {...item, cancelling: true, cancelError: null} : item)});
    try {
      const result = await cancelFireRiskJob(reference, current.controller.signal);
      if (reader.current !== current || current.controller.signal.aborted) return;
      setState(previous => previous.owner !== mapData ? previous : {...previous, jobs: previous.jobs.map(item =>
        item.reference.job_id === jobId ? {...item, result, cancelling: false, cancelError: null} : item)});
      // Rejoin the saved read workflow, including a completion that won the cancellation race.
      refreshStatus();
    } catch (error) {
      if (reader.current !== current || current.controller.signal.aborted) return;
      setState(previous => previous.owner !== mapData ? previous : {...previous, jobs: previous.jobs.map(item =>
        item.reference.job_id === jobId ? {...item, cancelling: false,
          cancelError: error instanceof Error ? error.message : "Cancellation status could not be read."} : item)});
    }
  }, [mapData, refreshStatus, state]);

  const retryJob = useCallback(async (jobId: string): Promise<void> => {
    const current = reader.current;
    const reference = state.owner === mapData
      ? state.jobs.find(item => item.reference.job_id === jobId)?.reference : undefined;
    if (!reference || !current || current.owner !== mapData || current.controller.signal.aborted) return;
    const intent = intents.current.get(jobId) ?? {
      requestId: savedRetryIntent(reference)?.requestId ?? crypto.randomUUID(), pending: false};
    if (intent.pending) return;
    intent.pending = true;
    intents.current.set(jobId, intent);
    saveRetryIntent(reference, {requestId: intent.requestId});
    setState(previous => previous.owner !== mapData ? previous : {...previous, jobs: previous.jobs.map(item =>
      item.reference.job_id === jobId ? {...item, retrying: true, retryError: null} : item)});
    try {
      const result = await retryFireRiskJob(reference, intent.requestId, current.controller.signal);
      saveRetryIntent(reference, {requestId: intent.requestId,
        reference: {job_id: result.job_id, fire_refs: result.fire_refs}});
      if (reader.current !== current || current.controller.signal.aborted) return;
      setRetries(previous => ({owner: mapData, references: [...(previous.owner === mapData
        ? previous.references.filter(item => item.job_id !== result.job_id) : []),
      {job_id: result.job_id, fire_refs: result.fire_refs}]}));
    } catch (error) {
      if (reader.current !== current || current.controller.signal.aborted) return;
      setState(previous => previous.owner !== mapData ? previous : {...previous, jobs: previous.jobs.map(item =>
        item.reference.job_id === jobId ? {...item, retryError: error instanceof Error
          ? error.message : "Terrain retry could not be read."} : item)});
    } finally {
      intent.pending = false;
      setState(previous => previous.owner !== mapData ? previous : {...previous, jobs: previous.jobs.map(item =>
        item.reference.job_id === jobId ? {...item, retrying: false} : item)});
    }
  }, [mapData, state]);

  // Effects run after rendering: never expose the previous answer's results during that gap.
  return state.owner === mapData ? { assessments: state.assessments, jobs: state.jobs, refreshStatus, cancelJob, retryJob }
    : { assessments: mapData?.fireExposureData ?? [], jobs: (mapData?.fireExposureJobs ?? [])
      .map(reference => ({ reference, result: null, readError: null })), refreshStatus, cancelJob, retryJob };
}
