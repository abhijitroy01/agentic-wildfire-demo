/**
 * useRadioStream Hook
 * Reads the broker's radio stream: POST /simulation-api/v1/radio/stream plays the stored recording and answers
 * with server-sent events, one {type, data} message per data frame (backend/radio_broker.py).
 */
import { brokerFetch } from "@/shared/services/workspaceAccess";
import { useCallback,useEffect,useRef,useState } from "react";
import type { LogEntry,RadioStreamMessage } from "../types";

export interface UseRadioStreamOptions {
  /** Handler for parsed stream messages */
  onMessage: (msg: RadioStreamMessage) => void;
  /** Optional handler for the broker accepting the stream */
  onConnected?: () => void;
  /** Optional handler for a failed stream; disconnect() reports nothing */
  onError?: (error: string) => void;
}

export interface UseRadioStreamResult {
  /** Start the stream, ending any earlier one */
  connect: () => void;
  /** End the stream */
  disconnect: () => void;
  /** Error message if any */
  errorMessage: string | null;
  /** Log entries */
  logs: LogEntry[];
  /** Add a log entry */
  addLog: (level: string, message: string) => void;
  /** Reset state (logs, error) */
  resetState: () => void;
}

/**
 * Stream hook for the broker's radio playback.
 * Handles the stream lifecycle, logging, and message parsing.
 */
export function useRadioStream(options: UseRadioStreamOptions): UseRadioStreamResult {
  const { onMessage, onConnected, onError } = options;

  // State
  const [errorMessage, setErrorMessage] = useState<string | null>(null);
  const [logs, setLogs] = useState<LogEntry[]>([]);

  // Refs
  // The running stream's controller: connect(), disconnect() and unmount abort it, so a stale stream reports nothing.
  const streamAbort = useRef<AbortController | null>(null);
  const onMessageRef = useRef(onMessage);
  const onConnectedRef = useRef(onConnected);
  const onErrorRef = useRef(onError);

  // Keep callback refs updated
  useEffect(() => {
    onMessageRef.current = onMessage;
  }, [onMessage]);
  useEffect(() => {
    onConnectedRef.current = onConnected;
  }, [onConnected]);
  useEffect(() => {
    onErrorRef.current = onError;
  }, [onError]);

  // Add log entry
  const addLog = useCallback((level: string, message: string): void => {
    const now = new Date();
    const time = now.toTimeString().split(" ")[0];
    setLogs((prev) => [...prev, { time, level, message }]);
  }, []);

  // Reset state
  const resetState = useCallback((): void => {
    setLogs([]);
    setErrorMessage(null);
  }, []);

  // Start the stream
  const connect = useCallback((): void => {
    streamAbort.current?.abort();
    const abort = new AbortController();
    streamAbort.current = abort;
    setErrorMessage(null);
    void (async () => {
      let terminal = false;
      let stalled = false;
      let reader: ReadableStreamDefaultReader<Uint8Array> | undefined;
      let idle: ReturnType<typeof setTimeout> | undefined;
      // The broker sends a heartbeat every two seconds even during long transcription/model work.
      const armIdle = (): void => {
        clearTimeout(idle);
        idle = setTimeout(() => { stalled = true; abort.abort(); void reader?.cancel().catch(() => undefined); }, 30_000);
      };
      const stopRead = (): void => { void reader?.cancel().catch(() => undefined); };
      abort.signal.addEventListener("abort", stopRead, {once: true});
      armIdle();
      try {
        const response = await brokerFetch("/radio/stream", {
          method: "POST", signal: abort.signal, body: JSON.stringify({ playback: true }),
        });
        if (abort.signal.aborted) {
          if (stalled) throw new Error("Radio stream did not respond in time. Please retry.");
          return;
        }
        if (!response.body) throw new Error("Radio stream unavailable.");
        onConnectedRef.current?.();
        reader = response.body.getReader();
        const decoder = new TextDecoder();
        let buffer = "";
        try {
          while (!abort.signal.aborted) {
            const chunk = await reader.read(); if (chunk.done || abort.signal.aborted) break;
            armIdle();
            buffer += decoder.decode(chunk.value, { stream: true });
            const frames = buffer.replace(/\r\n/g, "\n").split("\n\n"); buffer = frames.pop() || "";
            for (const frame of frames) {
              const data = frame.split("\n").filter(line => line.startsWith("data:"))
                .map(line => line.slice(5).trimStart()).join("\n");
              if (data) {
                const message = JSON.parse(data) as RadioStreamMessage;
                if (!message || typeof message !== "object" || typeof message.type !== "string") {
                  throw new Error("Radio stream returned an unusable event.");
                }
                onMessageRef.current(message);
                if (message.type === "complete" || message.type === "error") { terminal = true; break; }
              }
            }
            if (terminal) break;
          }
        } finally { await reader.cancel().catch(() => undefined); reader.releaseLock(); }
        if (stalled) throw new Error("Radio stream stopped responding. Please retry.");
        if (!abort.signal.aborted && !terminal) throw new Error(
          "Radio processing ended before a final result was received. Please retry.");
      } catch (error) {
        if (abort.signal.aborted && !stalled) return;
        const message = stalled ? "Radio stream stopped responding. Please retry."
          : error instanceof Error ? error.message : "Radio stream failed.";
        setErrorMessage(message); onErrorRef.current?.(message);
      } finally {
        clearTimeout(idle);
        abort.signal.removeEventListener("abort", stopRead);
      }
    })();
  }, []);

  // End the stream
  const disconnect = useCallback((): void => {
    streamAbort.current?.abort();
  }, []);

  // End the stream on unmount
  useEffect(() => () => streamAbort.current?.abort(), []);

  return {
    connect,
    disconnect,
    errorMessage,
    logs,
    addLog,
    resetState,
  };
}
