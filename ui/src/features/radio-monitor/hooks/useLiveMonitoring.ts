/**
 * useLiveMonitoring — drives stage 2's "Play radio & investigate".
 *
 * The broker's radio stream (useRadioStream) plays the stored recording through the private radio service, which
 * transcribes it and scans it for fire keywords. We listen for `connected`, `simulation_audio`, `log`, `step_*`,
 * `detection`, `complete`, and `error` messages. The first detection of a run hands its address to the caller's
 * strategy, which resolves it and prepares the incident; every step is added to the processing log.
 */
import logger from "@/shared/utils/logger";
import { useCallback,useEffect,useMemo,useRef,useState } from "react";
import type { Detection,LogEntry,RadioStreamMessage } from "../types";
import { useRadioStream } from "./useRadioStream";

// ---------------------------------------------------------------------------
// Public types
// ---------------------------------------------------------------------------

/** What a radio run does with its results: prepare the detected address, and show the recording and transcript. */
export interface RadioStrategy {
  onDetected: (address: string) => Promise<void>;
  onAudio?: (url: string, file: string) => void;
  onTranscript?: (text: string) => void;
}

export interface UseLiveMonitoringResult {
  errorMessage: string | null;
  logs: LogEntry[];
  detection: Detection | null;
  isMonitoring: boolean;
  startMonitoring: () => void;
  stopMonitoring: () => void;
  /** Forget the last detection when a scenario starts from another address; playback and logs are unchanged. */
  clearDetection: () => void;
}

// ---------------------------------------------------------------------------
// Hook
// ---------------------------------------------------------------------------

/** React hook running stage 2's radio investigation and reporting detected addresses and transcripts. */
export function useLiveMonitoring(strategy: RadioStrategy): UseLiveMonitoringResult {
  const strategyRef = useRef(strategy);
  const detectionHandled = useRef(false);
  strategyRef.current = strategy;
  const [detection, setDetection] = useState<Detection | null>(null);
  const detectionRef = useRef<Detection | null>(null);
  const [isMonitoring, setIsMonitoring] = useState(false);
  // The message of the error event that ended the run; shown as the error message until the next run starts.
  const [runError, setRunError] = useState<string | null>(null);

  // A new run, Stop, a stream failure or unmount makes a pending investigation's outcome stale.
  const generationRef = useRef(0);
  const invalidateInvestigation = useCallback((): void => { ++generationRef.current; }, []);

  // -------------------------------------------------------------------------
  // Investigation
  // -------------------------------------------------------------------------

  const startInvestigation = useCallback(
    (location: string, addLog: (level: string, message: string) => void) => {
      const generation = ++generationRef.current;
      const isCurrent = (): boolean => generation === generationRef.current;
      addLog("INFO", `Investigating fire at ${location} via FireguardAI...`);
      strategyRef.current.onDetected(location).then(() => {
        if (!isCurrent()) return;
        setIsMonitoring(false);
        addLog("INFO", "Location verified; incident investigation and road preparation started. Review the draft before running.");
      }).catch((err: unknown) => {
        if (!isCurrent()) return;
        setIsMonitoring(false);
        addLog("ERROR", err instanceof Error ? err.message : "Simulation investigation failed");
      });
    },
    [],
  );

  // -------------------------------------------------------------------------
  // Radio stream plumbing
  // -------------------------------------------------------------------------

  const handleMessage = useCallback(
    (msg: RadioStreamMessage): void => {
      const { type, data } = msg;

      switch (type) {
        case "connected":
          stream.addLog("INFO", "Session ready, starting audio processing...");
          break;

        case "simulation_audio":
          if (data?.url) strategyRef.current.onAudio?.(data.url, data.file || "Emergency radio recording");
          break;

        case "log":
          stream.addLog(data?.level || "INFO", data?.message || "");
          break;

        case "step_start":
          stream.addLog("INFO", `Step: ${data?.name}`);
          break;

        case "step_complete":
          stream.addLog("INFO", `Completed: ${data?.name || "step"}`);
          break;

        case "detection": {
          // Only a run's first detection starts an investigation.
          if (detectionHandled.current) break;
          const supplied = typeof data?.location === "string" ? data.location.trim() : "";
          const location = supplied && supplied.toLowerCase() !== "unknown" ? supplied : null;
          const newDetection: Detection = {
            location,
            keywords: Array.isArray(data?.keywords) ? data.keywords.filter(item => typeof item === "string") : [],
            confidence: typeof data?.confidence === "number" && Number.isFinite(data.confidence)
              && data.confidence >= 0 && data.confidence <= 1 ? data.confidence : null,
            timestamp: new Date().toISOString(),
            audioFile: data?.source_id || data?.audio_file || "Unknown",
          };

          setDetection(newDetection);
          detectionRef.current = newDetection;
          if (!location) {
            stream.addLog("WARN", "Fire-related audio detected; waiting for a usable address.");
            break;
          }
          detectionHandled.current = true;
          stream.addLog("WARN", `🔥 FIRE DETECTED: ${location}`);
          stream.addLog(
            "INFO",
            `Keywords: ${newDetection.keywords.length ? newDetection.keywords.join(", ") : "unavailable"}`,
          );
          stream.addLog(
            "INFO",
            `Confidence: ${newDetection.confidence === null ? "unavailable"
              : `${(newDetection.confidence * 100).toFixed(0)}%`}`,
          );

          strategyRef.current.onTranscript?.(data?.transcript_excerpt || "");
          startInvestigation(location, stream.addLog);
          break;
        }

        case "complete": {
          const hadDetection = detectionHandled.current;
          const observed = detectionRef.current;
          if (data?.first_detection?.location) {
            stream.addLog(
              "INFO",
              `Monitoring complete — fire detected at ${data.first_detection.location}`,
            );
          } else if (observed?.location) {
            stream.addLog(
              "INFO",
              `Monitoring complete — fire detected at ${observed.location}`,
            );
          } else {
            stream.addLog("INFO", observed ? "Monitoring complete — no usable fire address detected"
              : "Monitoring complete — no fire detected");
          }
          if (!hadDetection) setIsMonitoring(false);
          break;
        }

        case "error": {
          // The broker sends its own fixed text; it ends the run, so it also becomes the alert, not only a log line.
          const message = data?.message || "Unknown error";
          stream.addLog("ERROR", message);
          setRunError(message);
          setIsMonitoring(false);
          ++generationRef.current;
          break;
        }

        default:
          // The type only: the payload can carry transcript text and detection details.
          logger.log("Unknown message type:", type);
      }
    },
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [detection, startInvestigation],
  );

  const stream = useRadioStream({
    onMessage: handleMessage,
    onConnected: () => {
      stream.addLog("INFO", "🚀 Connected to monitoring service");
    },
    onError: (error) => {
      ++generationRef.current;
      stream.addLog("ERROR", `❌ ${error}`);
      setIsMonitoring(false);
    },
  });

  // -------------------------------------------------------------------------
  // Lifecycle
  // -------------------------------------------------------------------------

  const startMonitoring = useCallback((): void => {
    ++generationRef.current;
    stream.resetState();
    setRunError(null);
    detectionHandled.current = false;
    detectionRef.current = null;
    setDetection(null);
    setIsMonitoring(true);

    stream.addLog("INFO", "🚀 Starting Broadcastify Monitor...");
    stream.connect();
  }, [stream]);

  const stopMonitoring = useCallback((): void => {
    ++generationRef.current;
    stream.disconnect();
    setIsMonitoring(false);
  }, [stream]);

  const clearDetection = useCallback((): void => { detectionRef.current = null; setDetection(null); }, []);

  // An investigation that settles after unmount changes nothing.
  useEffect(() => invalidateInvestigation, [invalidateInvestigation]);

  return useMemo(
    () => ({
      errorMessage: runError ?? stream.errorMessage,
      logs: stream.logs,
      detection,
      isMonitoring,
      startMonitoring,
      stopMonitoring,
      clearDetection,
    }),
    [
      runError,
      stream.errorMessage,
      stream.logs,
      detection,
      isMonitoring,
      startMonitoring,
      stopMonitoring,
      clearDetection,
    ],
  );
}
