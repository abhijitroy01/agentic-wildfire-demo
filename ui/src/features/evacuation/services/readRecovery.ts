import { BrokerError } from "@/shared/services/workspaceAccess";
export { isTransientReadError as temporaryRead } from "@/shared/services/workspaceAccess";
import type { RunStatus } from "../domain";

const ORDER: Record<RunStatus, number> = {
  queued: 0, running: 1, cancelling: 2, completed: 3, cancelled: 3, failed: 3,
};

/** A late status read cannot undo cancellation or replace an already terminal receipt. */
export function newerStatus<T extends { status: RunStatus }>(previous: T, next: T): T {
  return ORDER[previous.status] === 3 || ORDER[next.status] < ORDER[previous.status] ? previous : next;
}

export function readDelay(error: unknown, attempt = 0): number {
  const advertised = error instanceof BrokerError ? (error.retryAfter ?? 0) * 1000 : 0;
  return Math.min(30_000, Math.max(advertised, 2500 * 2 ** attempt));
}
