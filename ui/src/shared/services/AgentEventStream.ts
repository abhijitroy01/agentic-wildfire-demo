/**
 * AG-UI event stream parser.
 *
 * Reads a ReadableStream of UTF-8 bytes that contains Server-Sent Events
 * formatted per the AG-UI spec (https://docs.ag-ui.com) and dispatches typed
 * events to the supplied callbacks.
 *
 * Supported event types (Phase 1):
 * - RUN_STARTED, RUN_FINISHED, RUN_ERROR
 * - TEXT_MESSAGE_START, TEXT_MESSAGE_CONTENT, TEXT_MESSAGE_END
 * - TOOL_CALL_START, TOOL_CALL_ARGS, TOOL_CALL_END, TOOL_CALL_RESULT
 * - STATE_SNAPSHOT  (carries the validated UniversalResponse from backend)
 *
 * All other AG-UI event types are silently ignored.
 */

import { logger } from "@/shared/utils/logger";

// AG-UI event types are emitted by the backend in either snake_case or
// camelCase depending on serializer config. We normalize on read.
export type AgUiEventType =
  | "RUN_STARTED"
  | "RUN_FINISHED"
  | "RUN_ERROR"
  | "TEXT_MESSAGE_START"
  | "TEXT_MESSAGE_CONTENT"
  | "TEXT_MESSAGE_END"
  | "TOOL_CALL_START"
  | "TOOL_CALL_ARGS"
  | "TOOL_CALL_END"
  | "TOOL_CALL_RESULT"
  | "STATE_SNAPSHOT"
  | "STATE_DELTA"
  | "MESSAGES_SNAPSHOT"
  | "REASONING_START"
  | "REASONING_MESSAGE_START"
  | "REASONING_MESSAGE_CONTENT"
  | "REASONING_MESSAGE_END"
  | "REASONING_END"
  | "CUSTOM";

export interface AgUiEvent {
  type: AgUiEventType | string;
  // Lifecycle
  threadId?: string;
  runId?: string;
  // Errors
  message?: string;
  code?: string;
  // Text streaming
  messageId?: string;
  role?: string;
  delta?: string;
  // Tool calls
  toolCallId?: string;
  toolCallName?: string;
  parentMessageId?: string;
  content?: string;
  // State
  snapshot?: unknown;
  patches?: unknown;
  [k: string]: unknown;
}

export interface AgentRunCallbacks {
  onRunStarted?: (e: AgUiEvent) => void;
  onRunFinished?: (e: AgUiEvent) => void;
  onRunError?: (message: string, code?: string) => void;
  onTextStart?: (messageId: string) => void;
  onTextDelta?: (messageId: string, delta: string) => void;
  onTextEnd?: (messageId: string) => void;
  onToolStart?: (toolCallId: string, toolCallName: string) => void;
  onToolArgs?: (toolCallId: string, delta: string) => void;
  onToolEnd?: (toolCallId: string) => void;
  onToolResult?: (toolCallId: string, content: string) => void;
  onSnapshot?: (snapshot: unknown) => void;
  /** Catch-all for unhandled types (handy for debugging). */
  onUnknown?: (event: AgUiEvent) => void;
}

/** An authoritative broker failure differs from a disconnected, incomplete stream. */
export class AgentRunFailure extends Error {
  readonly code: string | null;

  constructor(message: string, code?: string) {
    super(message);
    this.name = "AgentRunFailure";
    this.code = typeof code === "string" ? code : null;
  }
}

/** Convert any string to an UPPER_SNAKE_CASE AgUiEventType-style identifier. */
function normalizeType(raw: unknown): string {
  if (typeof raw !== "string") return "";
  // Already SCREAMING_SNAKE? keep it.
  if (/^[A-Z][A-Z0-9_]+$/.test(raw)) return raw;
  // camelCase or PascalCase → SCREAMING_SNAKE_CASE
  return raw
    .replace(/([a-z0-9])([A-Z])/g, "$1_$2")
    .replace(/[-\s]+/g, "_")
    .toUpperCase();
}

/** Normalize a parsed event so callers can read camelCase fields uniformly. */
function normalizeEvent(raw: Record<string, unknown>): AgUiEvent {
  const out: AgUiEvent = { type: normalizeType(raw.type) };
  for (const [k, v] of Object.entries(raw)) {
    if (k === "type") continue;
    // Convert snake_case → camelCase keys
    const camel = k.replace(/_([a-z])/g, (_m, c: string) => c.toUpperCase());
    (out as Record<string, unknown>)[camel] = v;
  }
  return out;
}

/** Route one AG-UI event to the matching run callback by event type. */
function dispatch(evt: AgUiEvent, cb: AgentRunCallbacks): void {
  switch (evt.type) {
    case "RUN_STARTED":
      cb.onRunStarted?.(evt);
      return;
    case "RUN_FINISHED":
      cb.onRunFinished?.(evt);
      return;
    case "RUN_ERROR":
      cb.onRunError?.(evt.message || "Unknown error", evt.code);
      return;
    case "TEXT_MESSAGE_START":
      cb.onTextStart?.(evt.messageId || "");
      return;
    case "TEXT_MESSAGE_CONTENT":
      cb.onTextDelta?.(evt.messageId || "", evt.delta || "");
      return;
    case "TEXT_MESSAGE_END":
      cb.onTextEnd?.(evt.messageId || "");
      return;
    case "TOOL_CALL_START":
      cb.onToolStart?.(evt.toolCallId || "", evt.toolCallName || "");
      return;
    case "TOOL_CALL_ARGS":
      cb.onToolArgs?.(evt.toolCallId || "", evt.delta || "");
      return;
    case "TOOL_CALL_END":
      cb.onToolEnd?.(evt.toolCallId || "");
      return;
    case "TOOL_CALL_RESULT":
      cb.onToolResult?.(evt.toolCallId || "", evt.content || "");
      return;
    case "STATE_SNAPSHOT":
      cb.onSnapshot?.(evt.snapshot);
      return;
    default:
      cb.onUnknown?.(evt);
  }
}

/**
 * Read an AG-UI SSE stream end-to-end, dispatching each frame as soon as its blank line arrives.
 * Lines may end in "\n" or "\r\n". Resolves when the stream closes (regardless of whether RUN_FINISHED arrived).
 */
export async function readAgUiStream(
  stream: ReadableStream<Uint8Array>,
  callbacks: AgentRunCallbacks,
  options: { signal?: AbortSignal } = {},
): Promise<void> {
  const reader = stream.getReader();
  const decoder = new TextDecoder("utf-8");
  let buf = "";

  const onAbort = (): void => {
    reader.cancel().catch(() => undefined);
  };
  if (options.signal) {
    if (options.signal.aborted) onAbort();
    options.signal.addEventListener("abort", onAbort, { once: true });
  }

  try {
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      // Normalize CRLF line endings across the whole buffer, so a "\r\n" split between two chunks still
      // joins and a CRLF blank line ends its frame now rather than when the stream closes.
      buf = (buf + decoder.decode(value, { stream: true })).replace(/\r\n/g, "\n");

      // SSE frames are separated by "\n\n". Process all complete frames in the buffer.
      let sep: number;
      while ((sep = buf.indexOf("\n\n")) !== -1) {
        const frame = buf.slice(0, sep);
        buf = buf.slice(sep + 2);
        processFrame(frame, callbacks);
      }
    }
    // Flush any trailing frame
    if (buf.trim()) processFrame(buf, callbacks);
  } catch (error) {
    await reader.cancel().catch(() => undefined);
    throw error;
  } finally {
    if (options.signal) options.signal.removeEventListener("abort", onAbort);
    reader.releaseLock();
  }
}

/**
 * A completed investigation requires the broker's saved snapshot and successful
 * terminal event. Progress text and a snapshot followed by failure are not results.
 */
export async function readFinalAgentSnapshot(
  stream: ReadableStream<Uint8Array>,
  callbacks: AgentRunCallbacks,
  options: { signal?: AbortSignal } = {},
): Promise<unknown> {
  let snapshot: unknown;
  let finished = false;
  await readAgUiStream(stream, {
    ...callbacks,
    onSnapshot: value => { snapshot = value; },
    onRunFinished: () => { finished = true; },
    onRunError: (message, code) => { throw new AgentRunFailure(message, code); },
  }, options);
  if (!finished || !snapshot || typeof snapshot !== "object") {
    throw new Error("Investigation ended without a completed saved response. Retry the question.");
  }
  return snapshot;
}

/** Parse one server-sent-events frame's data lines as JSON and dispatch the AG-UI event. */
function processFrame(frame: string, cb: AgentRunCallbacks): void {
  const lines = frame.split("\n");
  const dataLines: string[] = [];
  for (const line of lines) {
    if (line.startsWith("data:")) {
      dataLines.push(line.slice(5).trimStart());
    }
    // Ignore "event:", "id:", ":" comments etc — AG-UI puts everything in data.
  }
  if (dataLines.length === 0) return;
  const payload = dataLines.join("\n").trim();
  if (!payload) return;

  let parsed: unknown;
  try {
    parsed = JSON.parse(payload);
  } catch {
    // Skip malformed frames. Log the size only: frames carry the visitor's question and the agent's answer.
    logger.warn("[AgUiStream] Skipping a non-JSON SSE frame:", `${payload.length} characters`);
    return;
  }
  if (!parsed || typeof parsed !== "object") return;

  const evt = normalizeEvent(parsed as Record<string, unknown>);
  dispatch(evt, cb);
}
