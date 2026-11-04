/**
 * AgentService - Bridge between the React UI and the AgentCore Runtime.
 *
 * The runtime speaks AG-UI (Agent-User Interaction Protocol) over Server-
 * Sent Events. The single public entry point is `invokeAgentStreaming()`
 * which returns a handle the caller can use to receive live tool-call
 * status, the final `UniversalResponse` snapshot, or an error. See
 * `AgentEventStream.ts` for the parser.
 */

import { MESSAGES,TIMING } from "@/shared/constants";
import AgentResponseParser,{
type ParsedResponse,
} from "@/shared/services/agentResponseParser";
import { logger } from "@/shared/utils/logger";
import {
AgentInvocationError,
AgentParseError,
AgentTimeoutError,
} from "../utils/errors";
import { AgentRunFailure, readFinalAgentSnapshot,type AgentRunCallbacks } from "./AgentEventStream";
import { labelForTool } from "./agentToolLabels";
import { brokerFetch, BrokerError, isTransientReadError } from "./workspaceAccess";
import {
  newInvestigation, retainInvestigation, acknowledgeInvestigation, forgetInvestigation, readInvestigation, recoveryDelay,
  type PendingInvestigation,
} from "./investigationRecovery";

// ============================================================================
// Public types
// ============================================================================

export interface InvokeAgentStreamingCallbacks {
  /** Fires when AG-UI emits RUN_STARTED (run is on the wire). */
  onRunStarted?: () => void;
  /**
   * Real-time status string derived from TOOL_CALL_START events.
   * Convenience wrapper around `onToolStart` for callers that only need
   * a flat "what's happening right now" line (e.g. the chat panel).
   */
  onLoadingMessage?: (message: string) => void;
  /**
   * Structured tool-call lifecycle events. Use these when you need to
   * render a list of completed actions (e.g. Investigation's Agent
   * activity). `label` is the human-friendly string from
   * `agentToolLabels.labelForTool(name)`.
   */
  onToolStart?: (toolCallId: string, toolName: string, label: string) => void;
  /** Tool arguments are complete; this does not establish execution success. */
  onToolEnd?: (toolCallId: string) => void;
  onToolResult?: (toolCallId: string, content: unknown) => void;
  /** Fires once the supervisor's final UniversalResponse is parsed. */
  onComplete?: (parsed: ParsedResponse) => void;
  /** Fires on any error along the stream. */
  onError?: (err: Error) => void;
}

export interface InvokeAgentStreamingHandle {
  /** Abort the in-flight invocation. Idempotent. */
  cancel: () => void;
}

// ============================================================================
// Streaming invocation
// ============================================================================

/**
 * Invoke the supervisor and stream AG-UI events back to the caller.
 *
 * The function returns immediately with a handle; the actual work happens
 * asynchronously and reports progress through the supplied callbacks.
 */
export function invokeAgentStreaming(
  userInput: string,
  callbacks: InvokeAgentStreamingCallbacks,
  intent: PendingInvestigation = newInvestigation(userInput),
): InvokeAgentStreamingHandle {
  const controller = new AbortController();
  retainInvestigation(intent);

  void runStreaming(userInput, callbacks, controller, intent).catch(
    (err) => {
      const e = err instanceof Error ? err : new Error(String(err));
      try {
        callbacks.onError?.(e);
      } catch (cbErr) {
        logger.error("onError callback threw:", cbErr);
      }
    },
  );

  return {
    cancel: () => controller.abort(),
  };
}

/** Rejoin accepted work using only owned receipt reads; never replays the paid invocation. */
export function resumeAgentInvestigation(intent: PendingInvestigation,
  callbacks: InvokeAgentStreamingCallbacks): InvokeAgentStreamingHandle {
  const controller = new AbortController();
  void runStreaming(intent.question, callbacks, controller, intent, true).catch(error => {
    try { callbacks.onError?.(error instanceof Error ? error : new Error(String(error))); }
    catch (callbackError) { logger.error("onError callback threw:", callbackError); }
  });
  return {cancel: () => controller.abort()};
}

async function runStreaming(
  userInput: string,
  cb: InvokeAgentStreamingCallbacks,
  controller: AbortController,
  intent: PendingInvestigation,
  readOnly = false,
): Promise<void> {
  if (!userInput || typeof userInput !== "string") {
    throw new Error("Valid input is required");
  }

  // Hard timeout to guard against runaway streams.
  const timeoutHandle = setTimeout(
    () => controller.abort(),
    TIMING.AGENT_TIMEOUT_MS,
  );
  let recoveryStarted = readOnly;

  try {
    const complete = async (snapshot: unknown): Promise<void> => {
      const parsed = await parseSnapshot(snapshot);
      if (controller.signal.aborted) throw controller.signal.reason;
      forgetInvestigation(intent);
      cb.onComplete?.(parsed);
    };
    if (readOnly) {
      await complete(await recoverSnapshot(intent, cb, controller.signal));
      return;
    }
    // The broker forwards only the observations it retained (agent_broker.py), so the browser sends no context.
    const response = await brokerFetch("/investigation/turns", {
      method: "POST", signal: controller.signal,
      body: JSON.stringify({ request_id: intent.requestId, thread_id: intent.threadId,
        question: userInput, context_envelope: null }),
    });
    acknowledgeInvestigation(intent);
    if (!response.body) {
      recoveryStarted = true;
      await complete(await recoverSnapshot(intent, cb, controller.signal));
      return;
    }

    const streamCallbacks: AgentRunCallbacks = {
      onRunStarted: () => cb.onRunStarted?.(),
      onToolStart: (id, name) => {
        const label = labelForTool(name);
        cb.onLoadingMessage?.(label);
        cb.onToolStart?.(id, name, label);
      },
      onToolEnd: (id) => {
        cb.onToolEnd?.(id);
      },
      onToolResult: (id, content) => cb.onToolResult?.(id, content),
    };

    let snapshot: unknown;
    try {
      snapshot = await readFinalAgentSnapshot(response.body, streamCallbacks, {signal: controller.signal});
    } catch (error) {
      if (error instanceof AgentRunFailure) {
        forgetInvestigation(intent);
        throw error;
      }
      if (controller.signal.aborted) throw error;
      recoveryStarted = true;
      cb.onLoadingMessage?.("Connection interrupted; reading the accepted investigation…");
      snapshot = await recoverSnapshot(intent, cb, controller.signal);
    }
    await complete(snapshot);
  } catch (err) {
    let failure = err;
    if (controller.signal.aborted) {
      cb.onError?.(
        new AgentTimeoutError(MESSAGES.TIMEOUT_ERROR, TIMING.AGENT_TIMEOUT_MS),
      );
      return;
    }
    // A lost response may have accepted the POST. Reconcile even when no stream headers reached the browser.
    if (!recoveryStarted && isTransientReadError(err)) {
      try {
        const snapshot = await recoverSnapshot(intent, cb, controller.signal);
        const parsed = await parseSnapshot(snapshot);
        if (controller.signal.aborted) throw controller.signal.reason;
        forgetInvestigation(intent);
        cb.onComplete?.(parsed);
        return;
      } catch (recoveryError) { failure = recoveryError; }
    }
    cb.onError?.(failure instanceof AgentParseError || failure instanceof AgentInvocationError || failure instanceof AgentRunFailure
      ? failure : new AgentInvocationError(failure instanceof Error ? failure.message : "Investigation failed."));
  } finally {
    clearTimeout(timeoutHandle);
  }
}

async function recoverSnapshot(intent: PendingInvestigation, cb: InvokeAgentStreamingCallbacks,
  signal: AbortSignal): Promise<unknown> {
  let failures = 0;
  while (!signal.aborted) {
    try {
      const receipt = await readInvestigation(intent, signal);
      failures = 0;
      if (receipt.status === "completed") {
        return receipt.snapshot;
      }
      if (receipt.status !== "running") {
        forgetInvestigation(intent);
        throw new AgentInvocationError(receipt.error?.message ??
          `Investigation ${receipt.status}. A new attempt requires a new explicit request.`);
      }
      cb.onLoadingMessage?.("The accepted investigation is continuing; checking its retained result…");
      await recoveryDelay(2500, signal);
    } catch (error) {
      const missingBeforeAcknowledgement = error instanceof BrokerError && error.httpStatus === 404
        && intent.acknowledged !== true;
      if (error instanceof BrokerError && [404, 410].includes(error.httpStatus) && !missingBeforeAcknowledgement) {
        forgetInvestigation(intent);
      }
      if (signal.aborted || !(missingBeforeAcknowledgement || isTransientReadError(error)) || ++failures >= 5) {
        if (missingBeforeAcknowledgement && !signal.aborted) {
          throw new AgentInvocationError("Investigation acceptance is still unconfirmed. Check the same saved request again.");
        }
        throw error;
      }
      cb.onLoadingMessage?.(missingBeforeAcknowledgement
        ? "Acceptance is not confirmed yet; checking this same investigation request…"
        : "Investigation status is temporarily unavailable; checking the same accepted turn…");
      await recoveryDelay(error instanceof BrokerError && error.retryAfter !== null
        ? Math.max(1000, error.retryAfter * 1000) : 2500, signal);
    }
  }
  throw signal.reason;
}

// ============================================================================
// Helpers
// ============================================================================

// Re-exported for callers that consume ParsedResponse via this module.
export type { ParsedResponse };

interface MaybeUniversalResponse {
  type?: string;
  content?: Record<string, unknown>;
}

function parseSnapshot(snapshot: unknown): Promise<ParsedResponse> {
  if (!snapshot || typeof snapshot !== "object") {
    return Promise.reject(new Error("STATE_SNAPSHOT had no payload"));
  }
  const universal = snapshot as MaybeUniversalResponse;
  const json = JSON.stringify(universal);
  return AgentResponseParser.parse(json);
}
