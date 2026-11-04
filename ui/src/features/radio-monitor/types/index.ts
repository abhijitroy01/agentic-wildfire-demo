/**
 * Radio Investigation Types
 * The broker's radio stream messages, the processing log and a detected report
 */
// --- Log Entry ---

export interface LogEntry {
  time: string;
  level: string;
  message: string;
}

// --- Detection Types ---

export interface Detection {
  location: string | null;
  keywords: string[];
  confidence: number | null;
  timestamp: string;
  audioFile: string;
}

// --- Radio Stream Types ---

export interface RadioStreamMessageData {
  // simulation_audio
  url?: string;
  file?: string;
  // log, step_start, step_complete and error
  level?: string;
  message?: string;
  name?: string;

  // detection
  location?: string | null;
  keywords?: string[];
  confidence?: number;
  source_id?: string;
  audio_file?: string;
  transcript_excerpt?: string;

  // complete
  first_detection?: {
    location: string;
  };
}

/** One event of the broker's radio stream (backend/radio_broker.py). */
export interface RadioStreamMessage {
  type: string;
  data?: RadioStreamMessageData;
}
