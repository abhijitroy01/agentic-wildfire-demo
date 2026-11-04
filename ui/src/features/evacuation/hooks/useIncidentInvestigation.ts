import { useCallback, useEffect, useRef, useState } from "react";
import type { IncidentInvestigationResponse } from "../generated/contracts";
import { api } from "../services/api";

/** Read saved server work on navigation/reload. Only an explicit start may invoke agents. */
interface InvestigationState {
  initial: IncidentInvestigationResponse | null;
  updated: IncidentInvestigationResponse | null;
  error: string;
  start: (id: string, selectedRun?: string, retry?: boolean) => Promise<IncidentInvestigationResponse>;
  cancel: (selectedRun?: string) => Promise<void>;
}
export function useIncidentInvestigation(incidentId: string | undefined, runId?: string): InvestigationState {
  const [initial, setInitial] = useState<IncidentInvestigationResponse | null>(null);
  const [updated, setUpdated] = useState<IncidentInvestigationResponse | null>(null);
  const [error, setError] = useState("");
  const [refresh, setRefresh] = useState(0);
  const previousIncident = useRef(incidentId);
  const previousRun = useRef(runId);
  useEffect(() => {
    const changedIncident = previousIncident.current !== incidentId;
    if (changedIncident) {
      setInitial(null); previousIncident.current = incidentId;
    }
    if (changedIncident || previousRun.current !== runId) setUpdated(null);
    previousRun.current = runId;
    setError("");
    if (!incidentId) return;
    let disposed = false;
    let timer: number | undefined;
    const controller = new AbortController(), { signal } = controller;
    async function read(): Promise<void> {
      let delay = 30_000;
      try {
        const base = `/incidents/${incidentId}/investigation`;
        const [first, second] = await Promise.all([
          api<IncidentInvestigationResponse | null>(base, undefined, { signal }),
          runId ? api<IncidentInvestigationResponse | null>(`${base}?run_id=${encodeURIComponent(runId)}`, undefined,
            { signal }) : null,
        ]);
        if (first?.status === "running" || second?.status === "running") delay = 2000;
        if (!disposed) {
          setInitial(previous => JSON.stringify(previous) === JSON.stringify(first) ? previous : first);
          setUpdated(previous => JSON.stringify(previous) === JSON.stringify(second) ? previous : second);
          setError("");
        }
      } catch (err) {
        delay = 10_000;
        if (!disposed) setError(err instanceof Error ? err.message : "Cannot read saved investigation.");
      } finally { if (!disposed) timer = window.setTimeout(() => void read(), delay); }
    }
    void read();
    return () => { disposed = true; controller.abort(); window.clearTimeout(timer); };
  }, [incidentId, runId, refresh]);
  const start = useCallback(async (id: string, selectedRun?: string, retry = false) => {
    const task = await api<IncidentInvestigationResponse>(`/incidents/${id}/investigation`, {
      run_id: selectedRun || null, retry,
    });
    setRefresh(value => value+1);
    return task;
  }, []);
  const cancel = useCallback(async (selectedRun?: string) => {
    if (incidentId) {
      await api(`/incidents/${incidentId}/investigation/cancel`, {run_id: selectedRun || null});
      setRefresh(value => value+1);
    }
  }, [incidentId]);
  return { initial, updated, error, start, cancel };
}
