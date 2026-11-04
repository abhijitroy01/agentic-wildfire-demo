import { SessionManager } from "@/shared/utils/sessionManager";
import { tabStorage } from "./tabStorage";
import { brokerJson } from "./workspaceAccess";

export interface PendingInvestigation {
  requestId: string;
  threadId: string;
  question: string;
  /** Successful POST headers or a validated owned receipt establish acceptance. Absent on legacy intents. */
  acknowledged?: boolean;
}
export interface InvestigationReceipt {
  request_id: string;
  thread_id: string;
  status: "running" | "completed" | "failed" | "interrupted";
  snapshot: unknown;
  error: {code: string; message: string} | null;
}
const KEY = "fireguard_pending_investigation";
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

export function newInvestigation(question: string): PendingInvestigation {
  return {requestId: crypto.randomUUID(), threadId: SessionManager.getOrCreateSessionId(), question};
}
export function retainInvestigation(intent: PendingInvestigation): void {
  tabStorage.setItem(KEY, JSON.stringify(intent));
}
export function acknowledgeInvestigation(intent: PendingInvestigation): void {
  intent.acknowledged = true;
  const saved = pendingInvestigation();
  if (saved?.requestId === intent.requestId) retainInvestigation({...saved, acknowledged: true});
}
export function pendingInvestigation(): PendingInvestigation | null {
  const saved = tabStorage.getItem(KEY);
  if (!saved) return null;
  try {
    const intent = JSON.parse(saved) as PendingInvestigation;
    if (!UUID.test(intent.requestId) || typeof intent.threadId !== "string" || !intent.threadId
      || typeof intent.question !== "string" || !intent.question.trim()) throw new Error("Invalid saved investigation");
    return intent;
  } catch {
    tabStorage.removeItem(KEY);
    return null;
  }
}
export function forgetInvestigation(intent: PendingInvestigation): void {
  if (pendingInvestigation()?.requestId === intent.requestId) tabStorage.removeItem(KEY);
}
export async function readInvestigation(intent: PendingInvestigation, signal: AbortSignal): Promise<InvestigationReceipt> {
  const value = await brokerJson(`/investigation/turns/${encodeURIComponent(intent.requestId)}`,
    {signal, timeoutMs: 15_000}) as InvestigationReceipt;
  if (!value || value.request_id !== intent.requestId || value.thread_id !== intent.threadId
    || !["running", "completed", "failed", "interrupted"].includes(value.status)) {
    throw new Error("The retained investigation returned an unusable receipt.");
  }
  acknowledgeInvestigation(intent);
  return value;
}

export function recoveryDelay(ms: number, signal: AbortSignal): Promise<void> {
  if (signal.aborted) return Promise.reject(signal.reason);
  return new Promise((resolve, reject) => {
    const stop = (): void => { clearTimeout(timer); reject(signal.reason); };
    const timer = setTimeout(() => { signal.removeEventListener("abort", stop); resolve(); }, ms);
    signal.addEventListener("abort", stop, {once: true});
  });
}
