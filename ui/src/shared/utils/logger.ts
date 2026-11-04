/**
 * Centralized logging utility with environment-based levels
 */

const isDevelopment = import.meta.env.DEV;

export const logger = {
  log: (...args: unknown[]) => {
    if (isDevelopment) {
      console.log(...args);
    }
  },

  error: (...args: unknown[]) => {
    // Errors are always logged
    console.error(...args);
  },

  warn: (...args: unknown[]) => {
    if (isDevelopment) {
      console.warn(...args);
    }
  },

  debug: (...args: unknown[]) => {
    if (isDevelopment && import.meta.env.VITE_DEBUG === "true") {
      console.log("[DEBUG]", ...args);
    }
  },
};

export default logger;
