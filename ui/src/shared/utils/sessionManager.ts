/**
 * Session Management Utility for Browser Tab Isolation
 *
 * Uses sessionStorage to ensure each browser tab/window gets its own isolated session.
 * Sessions persist across page reloads within the same tab but are separate across tabs.
 */

import { logger } from "@/shared/utils/logger";
import { tabStorage } from "@/shared/services/tabStorage";

export interface SessionInfo {
  sessionId: string;
  createdAt: string;
  tabId: string;
}

export class SessionManager {
  private static readonly STORAGE_KEY = "agentcore_session_id";
  private static readonly SESSION_INFO_KEY = "agentcore_session_info";

  /**
   * Get or create session ID for current browser tab
   * Uses sessionStorage to ensure per-tab isolation
   */
  static getOrCreateSessionId(): string {
    try {
      // Check if session already exists for this tab
      let sessionId = tabStorage.getItem(this.STORAGE_KEY);

      if (!sessionId) {
        // Generate new session ID for this tab (AWS requires min 33 chars)
        const timestamp = Date.now();
        // Generate enough random characters by combining two random strings
        const random1 = Math.random().toString(36).substr(2);
        const random2 = Math.random().toString(36).substr(2);
        const random = (random1 + random2).substring(0, 15); // Take first 15 chars to guarantee length
        sessionId = `session_${timestamp}_${random}`;

        // Store session info
        const sessionInfo: SessionInfo = {
          sessionId: sessionId,
          createdAt: new Date().toISOString(),
          tabId: `tab_${timestamp}_${random}`,
        };

        tabStorage.setItem(this.STORAGE_KEY, sessionId);
        tabStorage.setItem(
          this.SESSION_INFO_KEY,
          JSON.stringify(sessionInfo)
        );

        logger.log(
          `🆔 NEW SESSION: Created for this browser tab: ${sessionId}`
        );
        logger.log(`📅 Session created at: ${sessionInfo.createdAt}`);
      } else {
        logger.log(
          `🆔 EXISTING SESSION: Reusing session for this tab: ${sessionId}`
        );

        // Log session info if available
        const sessionInfoStr = tabStorage.getItem(this.SESSION_INFO_KEY);
        if (sessionInfoStr) {
          try {
            const sessionInfo: SessionInfo = JSON.parse(sessionInfoStr);
            logger.log(
              `📅 Session originally created: ${sessionInfo.createdAt}`
            );
          } catch {
            logger.warn("Could not parse session info");
          }
        }
      }

      return sessionId;
    } catch (error) {
      logger.error(
        "SessionStorage not available, generating temporary session:",
        error
      );
      // Fallback for environments without sessionStorage (AWS requires min 33 chars)
      const timestamp = Date.now();
      const random = Math.random().toString(36).substr(2, 12); // Ensure min 33 chars total
      return `temp_session_${timestamp}_${random}`;
    }
  }

  /**
   * Clear current session (forces new session on next request)
   * Useful for "Start New Conversation" functionality
   */
  static clearSession(): void {
    try {
      const oldSessionId = tabStorage.getItem(this.STORAGE_KEY);
      tabStorage.removeItem(this.STORAGE_KEY);
      tabStorage.removeItem(this.SESSION_INFO_KEY);
      logger.log(`🗑️ CLEARED SESSION: Removed session ${oldSessionId}`);
    } catch (error) {
      logger.warn("Could not clear session storage:", error);
    }
  }

  /**
   * Get current session info for debugging
   */
  static getSessionInfo(): SessionInfo | null {
    try {
      const sessionInfoStr = tabStorage.getItem(this.SESSION_INFO_KEY);
      if (sessionInfoStr) {
        return JSON.parse(sessionInfoStr);
      }
      return null;
    } catch (error) {
      logger.warn("Could not retrieve session info:", error);
      return null;
    }
  }

  /**
   * Get current session ID without creating new one
   */
  static getCurrentSessionId(): string | null {
    try {
      return tabStorage.getItem(this.STORAGE_KEY);
    } catch {
      return null;
    }
  }
}

export default SessionManager;
