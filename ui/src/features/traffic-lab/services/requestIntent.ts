import { tabStorage } from "@/shared/services/tabStorage";

type Kind = "research" | "discovery";
export interface RequestIntent {
  readonly requestId: string;
  readonly body: string;
  readonly jobId?: string;
}
const key = (kind: Kind): string => `fireguard_traffic_${kind}_request`;

/** Save the UUID before POST; an absent acknowledgement retains the same body and identity across reloads. */
export function retainedIntent(kind: Kind, body: object): RequestIntent {
  const encoded = JSON.stringify(body);
  const saved = readIntent(kind);
  if (saved?.body === encoded && !saved.jobId) return saved;
  const next = {requestId: crypto.randomUUID(), body: encoded};
  tabStorage.setItem(key(kind), JSON.stringify(next));
  return next;
}

export function readIntent(kind: Kind): RequestIntent | null {
  try {
    const value = JSON.parse(tabStorage.getItem(key(kind)) || "null") as RequestIntent | null;
    return value && typeof value.requestId === "string" && /^[a-f0-9-]{36}$/i.test(value.requestId)
      && typeof value.body === "string" && (!value.jobId || /^[a-f0-9-]{36}$/i.test(value.jobId)) ? value : null;
  } catch { return null; }
}

export function acknowledge(kind: Kind, intent: RequestIntent, jobId: string): void {
  // A response to a retired study area cannot replace the newer area's durable recovery handle.
  if (readIntent(kind)?.requestId !== intent.requestId) return;
  tabStorage.setItem(key(kind), JSON.stringify({...intent, jobId}));
}

export function forgetIntent(kind: Kind): void { tabStorage.removeItem(key(kind)); }
