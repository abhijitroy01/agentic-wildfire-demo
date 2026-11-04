import { MESSAGES } from "@/shared/constants";
import type { ParsedResponse } from "@/shared/services/agentResponseParser";
import { useCallback,useState } from "react";

export interface Message {
  text: string;
  sender: "user" | "agent" | "system";
  id: string;
  timestamp?: string;
  evidence?: ParsedResponse;
  /** True while we are still streaming text into this bubble. */
  streaming?: boolean;
}

export interface UseChatReturn {
  messages: Message[];
  isLoading: boolean;
  setIsLoading: (loading: boolean) => void;
  addUserMessage: (text: string) => void;
  addErrorMessage: (error: Error | string) => void;

  // Streaming API (used by AG-UI flow)
  /**
   * Open a new agent bubble for an in-flight AG-UI run. While streaming we
   * accumulate the supervisor's text in `AgentService` (it's typically a
   * single JSON envelope, not human-readable until parsed) and swap it in
   * at the end via `replaceStreamingMessage`. We deliberately don't
   * incrementally render token deltas because the supervisor's structured
   * JSON output isn't sensible to show partially.
   */
  startStreamingMessage: (id: string) => void;
  /** Replace the streaming bubble's full text (used to swap raw JSON for the rendered response). */
  replaceStreamingMessage: (id: string, text: string) => void;
  /** Mark the streaming bubble as complete and attach the answer's evidence, if any. */
  finalizeStreamingMessage: (id: string, evidence?: ParsedResponse) => void;
}

const useChat = (): UseChatReturn => {
  const [messages, setMessages] = useState<Message[]>([
    {
      text: MESSAGES.WELCOME,
      sender: "agent",
      id: `welcome-${Date.now()}`,
    },
  ]);
  const [isLoading, setIsLoading] = useState(false);

  const addUserMessage = useCallback((text: string) => {
    setMessages((prev) => [
      ...prev,
      {
        text,
        sender: "user",
        id: `user-${Date.now()}`,
        timestamp: new Date().toISOString(),
      },
    ]);
  }, []);

  const addErrorMessage = useCallback((error: Error | string) => {
    const errorText = error instanceof Error ? error.message : String(error);
    setMessages((prev) => [
      ...prev,
      {
        text: `Error: ${errorText}`,
        sender: "system",
        id: `error-${Date.now()}`,
        timestamp: new Date().toISOString(),
      },
    ]);
  }, []);

  // ----- Streaming bubble helpers --------------------------------------------

  const startStreamingMessage = useCallback((id: string) => {
    setMessages((prev) => [
      ...prev,
      {
        id,
        text: "",
        sender: "agent",
        timestamp: new Date().toISOString(),
        streaming: true,
      },
    ]);
  }, []);

  const replaceStreamingMessage = useCallback((id: string, text: string) => {
    setMessages((prev) => prev.map((m) => (m.id === id ? { ...m, text } : m)));
  }, []);

  const finalizeStreamingMessage = useCallback(
    (id: string, evidence?: ParsedResponse) => {
      setMessages((prev) =>
        prev.map((m) =>
          m.id === id
            ? {
                ...m,
                streaming: false,
                evidence,
              }
            : m,
        ),
      );
    },
    [],
  );

  return {
    messages,
    isLoading,
    setIsLoading,
    addUserMessage,
    addErrorMessage,
    startStreamingMessage,
    replaceStreamingMessage,
    finalizeStreamingMessage,
  };
};

export default useChat;
