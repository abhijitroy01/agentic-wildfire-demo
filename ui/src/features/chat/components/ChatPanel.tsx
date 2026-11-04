/**
 * ChatPanel - Chat interface with message history and input form
 *
 * Accessibility Features:
 * - role="log" on messages container (auto-announcing live region)
 * - aria-live="polite" for screen reader announcements
 * - aria-label on interactive elements
 * - aria-busy on loading states
 * - Proper form labeling
 */
import type { Message } from "@/features/chat/hooks/useChat";
import type { ParsedResponse } from "@/shared/services/agentResponseParser";
import React,{ useEffect,useRef } from "react";

interface ChatPanelProps {
  messages: Message[];
  isLoading: boolean;
  submissionBlocked?: boolean;
  /** Live status string driven by AG-UI events (e.g. "Detection agent: querying fires"). */
  currentLoadingMessage: string;
  inputText: string;
  onInputChange: (text: string) => void;
  onSubmit: (e: React.FormEvent) => void;
  onInspectEvidence?: (answer: ParsedResponse) => void;
}

const ChatPanel: React.FC<ChatPanelProps> = ({
  messages,
  isLoading,
  submissionBlocked = false,
  currentLoadingMessage,
  inputText,
  onInputChange,
  onSubmit,
  onInspectEvidence,
}) => {
  const messagesEndRef = useRef<HTMLDivElement>(null);

  // Auto-scroll to latest message
  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages, isLoading]);

  return (
    <div className="chat-container" role="region" aria-label="Chat interface">
      <div
        className="messages-container"
        role="log"
        aria-live="polite"
        aria-label="Chat messages"
        aria-relevant="additions"
      >
        {messages.map((message, index) => (
          <article
            key={message.id || index}
            className={`message ${message.sender}`}
            aria-label={`Message from ${message.sender === "user" ? "you" : message.sender === "agent" ? "FireGuard AI" : "system"}`}
          >
            <div className="message-bubble">
              {message.text.split("\n").map((line, i) => (
                <React.Fragment key={i}>
                  {line}
                  {i < message.text.split("\n").length - 1 && <br />}
                </React.Fragment>
              ))}
              {message.evidence && onInspectEvidence && (
                <button className="workspace-button" onClick={() => onInspectEvidence(message.evidence!)}>
                  Inspect this answer’s evidence
                </button>
              )}
            </div>
          </article>
        ))}
        {isLoading && (
          <div
            className="loading-indicator"
            role="status"
            aria-busy="true"
            aria-label="Processing your request"
          >
            <div className="fire-spinner" aria-hidden="true"></div>
            <div className="loading-message">
              {currentLoadingMessage}
              <small>This may take 2-3 minutes for complex queries</small>
            </div>
            <div className="loading-progress" aria-hidden="true"></div>
          </div>
        )}
        {/* Scroll anchor for auto-scroll */}
        <div ref={messagesEndRef} aria-hidden="true" />
      </div>
      <form
        onSubmit={onSubmit}
        className="input-form"
        aria-label="Send a message"
      >
        <label htmlFor="chat-input" className="visually-hidden">
          Type your message
        </label>
        <input
          id="chat-input"
          type="text"
          value={inputText}
          onChange={(e) => onInputChange(e.target.value)}
          placeholder="Type your message here..."
          className="message-input"
          disabled={isLoading || submissionBlocked}
          aria-disabled={isLoading || submissionBlocked}
          aria-describedby={isLoading ? "loading-status" : undefined}
        />
        <button
          type="submit"
          className="send-button"
          disabled={isLoading || submissionBlocked}
          aria-disabled={isLoading || submissionBlocked}
          aria-label={isLoading ? "Sending message, please wait" : "Send message"}
        >
          {isLoading ? "Sending..." : "Send"}
        </button>
        {isLoading && (
          <span id="loading-status" className="visually-hidden">
            Processing your request. Please wait.
          </span>
        )}
      </form>
    </div>
  );
};

export default ChatPanel;
