/**
 * Typed Error Classes for FIREGUARD AI
 * Provides structured error handling with metadata
 */

/**
 * Base error class for Agent-related errors
 */
export class AgentError extends Error {
  constructor(message: string, public code: string, public details?: unknown) {
    super(message);
    this.name = "AgentError";
    Object.setPrototypeOf(this, AgentError.prototype);
  }
}

/**
 * Agent invocation error (API call failed)
 */
export class AgentInvocationError extends AgentError {
  constructor(
    message: string,
    public statusCode?: number,
    public requestId?: string,
    public metadata?: Record<string, unknown>
  ) {
    super(message, "INVOCATION_ERROR", metadata);
    this.name = "AgentInvocationError";
    Object.setPrototypeOf(this, AgentInvocationError.prototype);
  }
}

/**
 * Response parsing error
 */
export class AgentParseError extends AgentError {
  constructor(
    message: string,
    public rawContent?: string,
    public parseError?: Error
  ) {
    super(message, "PARSE_ERROR", { rawContent, parseError });
    this.name = "AgentParseError";
    Object.setPrototypeOf(this, AgentParseError.prototype);
  }
}

/**
 * Timeout error
 */
export class AgentTimeoutError extends AgentError {
  constructor(message: string, public timeoutMs: number) {
    super(message, "TIMEOUT", { timeoutMs });
    this.name = "AgentTimeoutError";
    Object.setPrototypeOf(this, AgentTimeoutError.prototype);
  }
}
