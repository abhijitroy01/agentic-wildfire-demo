import { logger } from "@/shared/utils/logger";

// Tab persistence is an enhancement. Keep the current page usable when browser policy denies storage.
const memory = new Map<string, string>();
const memoryOnly = new Set<string>();
let persistent = true;
let reported = false;
function unavailable(): void {
  persistent = false;
  if (!reported) {
    reported = true;
    logger.warn("Tab storage is unavailable; this page will keep its state in memory.");
  }
}

export const tabStorage: Pick<Storage, "getItem" | "setItem" | "removeItem"> = {
  getItem(key): string | null {
    if (memoryOnly.has(key)) return memory.get(key) ?? null;
    try {
      const value = sessionStorage.getItem(key);
      persistent = true;
      if (value === null) memory.delete(key); else memory.set(key, value);
      return value;
    } catch {
      unavailable();
      return memory.get(key) ?? null;
    }
  },
  setItem(key, value): void {
    memory.set(key, value);
    try { sessionStorage.setItem(key, value); memoryOnly.delete(key); persistent = true; }
    catch { memoryOnly.add(key); unavailable(); }
  },
  removeItem(key): void {
    memory.delete(key);
    try { sessionStorage.removeItem(key); memoryOnly.delete(key); persistent = true; }
    catch { memoryOnly.add(key); unavailable(); }
  },
};

/** Renewal reloads require a persistent marker, otherwise denied cookies can produce a reload loop. */
export function tabStorageIsPersistent(): boolean { return persistent; }
