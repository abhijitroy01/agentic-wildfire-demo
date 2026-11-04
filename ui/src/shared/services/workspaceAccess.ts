// Workspace access for the simulation views. There is no user sign-in: deployed visitors get an anonymous
// HttpOnly demo-session cookie from the broker, and local development serves one local researcher.
import { tabStorage, tabStorageIsPersistent } from "./tabStorage";

/** Same-origin path of the broker's v1 routes; stream URLs are built from it too. */
export const BROKER_PATH = "/simulation-api/v1";

/** How the broker serves this browser: a CloudFront demo session, local development, or not open. */
export type AccessMode = "public_demo" | "local" | "closed";
/** GET /v1/workspace-config: the access mode and which server-side services are bound. */
export type WorkspaceConfig = {
  readonly access_mode: AccessMode;
  readonly agent_configured?: boolean;
  readonly geocoder_configured?: boolean;
  /** The Delphire camera the broker serves ([camera] device_id and its key); map popups offer it only when true. */
  readonly camera_configured?: boolean;
};

/** The broker's typed failure remains available to the workflow that owns the blocked step. */
export class BrokerError extends Error {
  readonly code: string | null;
  readonly httpStatus: number;
  /** Seconds until the server permits another admission attempt; never a client timeout. */
  readonly retryAfter: number | null;
  readonly retryable: boolean | null;
  /** Validated broker correlation ID for support and server-log lookup. */
  readonly requestId: string | null;
  constructor(message: string, httpStatus: number, code: string | null, retryAfter: number | null,
    retryable: boolean | null = null, requestId: string | null = null) {
    super(message);
    this.name = "BrokerError";
    this.httpStatus = httpStatus; this.code = code; this.retryAfter = retryAfter;
    this.retryable = retryable;
    this.requestId = requestId;
  }
}
/** Read recovery may repeat these transport/service outcomes; invalid receipts and permanent refusals are not transient. */
export function isTransientReadError(error: unknown): boolean {
  if (error instanceof BrokerError && error.retryable !== null) return error.retryable;
  return error instanceof TypeError || error instanceof DOMException && error.name === "TimeoutError"
    || error instanceof BrokerError && [408, 429, 500, 502, 503, 504].includes(error.httpStatus);
}
function retryAfterSeconds(value: string | null): number | null {
  if (!value) return null;
  const seconds = Number(value);
  if (Number.isFinite(seconds) && seconds >= 0) return seconds;
  const instant = Date.parse(value);
  return Number.isFinite(instant) ? Math.max(0, Math.ceil((instant-Date.now())/1000)) : null;
}

let config: WorkspaceConfig | undefined;
let initializing: Promise<WorkspaceConfig> | null = null;
let renewal: Promise<never> | null = null;

// Set in this tab before the demo-session renewal reload and cleared by the next successful request, so a
// session the browser cannot keep (for example, cookies blocked) stops with an error instead of reloading forever.
const RENEWAL_KEY = "fireguard_demo_session_renewed";

export const headers = (): Record<string, string> => ({ "Content-Type": "application/json" });

/** A signal that aborts when any input aborts; AbortSignal.any where the browser has it. */
export function anySignal(signals: readonly AbortSignal[]): AbortSignal {
  if (typeof AbortSignal.any === "function") return AbortSignal.any([...signals]);
  const controller = new AbortController();
  for (const signal of signals) {
    if (signal.aborted) { controller.abort(signal.reason); break; }
    signal.addEventListener("abort", () => controller.abort(signal.reason), { once: true });
  }
  return controller.signal;
}

/** Read the workspace configuration once and, for a public demo, start or resume this browser's demo session. */
const INITIALIZATION_TIMEOUT_MS = 30_000;

/** A caller can leave shared initialization without cancelling the other callers' startup. */
function waitForSignal<T>(work: Promise<T>, signal?: AbortSignal | null): Promise<T> {
  if (!signal) return work;
  if (signal.aborted) return Promise.reject(signal.reason ?? new DOMException("Aborted", "AbortError"));
  return new Promise<T>((resolve, reject) => {
    const aborted = (): void => reject(signal.reason ?? new DOMException("Aborted", "AbortError"));
    signal.addEventListener("abort", aborted, { once: true });
    work.then(resolve, reject).finally(() => signal.removeEventListener("abort", aborted));
  });
}

export function initialize(signal?: AbortSignal): Promise<WorkspaceConfig> {
  if (!initializing) initializing = (async () => {
    const startup = AbortSignal.timeout(INITIALIZATION_TIMEOUT_MS);
    try {
      const response = await fetch(`${BROKER_PATH}/workspace-config`, { signal: startup });
      if (!response.ok) throw new Error("Simulation API is not connected. Check the deployment configuration.");
      const loaded: WorkspaceConfig = await response.json();
      if (!["local", "public_demo", "closed"].includes(loaded?.access_mode)) {
        throw new Error("The workspace returned an unusable configuration.");
      }
      config = loaded;
      if (loaded.access_mode === "closed") throw new Error("This workspace is not open to visitors yet.");
      if (loaded.access_mode === "public_demo") {
        const session = await fetch(`${BROKER_PATH}/demo/session`,
          { method: "POST", credentials: "same-origin", signal: startup });
        if (!session.ok) throw new Error("Could not initialize the demo session. Reload the CloudFront page.");
        if ((await session.json()).session_created) tabStorage.removeItem("fireguard_simulation_session");
      }
      return loaded;
    } catch (error) {
      if (startup.aborted) throw new BrokerError("Workspace startup did not respond in time. Try again.",
        408, "WORKSPACE_STARTUP_TIMEOUT", null);
      throw error;
    }
  })().catch(error => { initializing = null; throw error; });
  return waitForSignal(initializing, signal);
}

/** Renew an expired demo session and reload the page, at most once until a request succeeds again. */
function renewDemoSession(): Promise<never> {
  if (tabStorage.getItem(RENEWAL_KEY)) {
    return Promise.reject(new Error(
      "The demo session could not be renewed. Allow cookies for this site, then reload the page."));
  }
  tabStorage.setItem(RENEWAL_KEY, "true");
  if (!tabStorageIsPersistent()) return Promise.reject(new Error(
    "The demo session cannot be renewed automatically while tab storage is unavailable. Allow cookies and reload."));
  tabStorage.removeItem("fireguard_simulation_session");
  initializing = null;
  return initialize().then(() => {
    location.reload();
    throw new Error("Demo session refreshed. Reloading the workspace.");
  });
}

/** Pass a successful response through; renew an expired demo session once, or throw the server's message. */
export async function check(response: Response): Promise<Response> {
  if (response.status === 401 && config?.access_mode === "public_demo") {
    renewal ??= renewDemoSession(); // Requests that fail together share one renewal and one reload.
    return renewal;
  }
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    throw new BrokerError(body.error?.message || `Simulation request failed (${response.status}).`,
      response.status, typeof body.error?.code === "string" ? body.error.code : null,
      retryAfterSeconds(response.headers.get("Retry-After")),
      typeof body.error?.retryable === "boolean" ? body.error.retryable : null,
      typeof body.error?.request_id === "string"
        && /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(body.error.request_id)
        ? body.error.request_id : null);
  }
  if (config?.access_mode === "public_demo") tabStorage.removeItem(RENEWAL_KEY);
  return response;
}

/** Call a same-origin broker route (/simulation-api/v1/...) with this browser's demo session cookie. Streams use this
 * directly; JSON calls go through brokerJson, which bounds them. */
export async function brokerFetch(path: string, init: RequestInit): Promise<Response> {
  await initialize(init.signal ?? undefined);
  return check(await fetch(`${BROKER_PATH}${path}`, {
    ...init, credentials: "same-origin", headers: { ...headers(), ...init.headers },
  }));
}

/** One brokerJson request: the method (GET without a body, POST with one, unless set), a JSON body, the caller's
 * signal (for example an effect's AbortController), a timeout and the message it rejects with (by default, the
 * simulation service did not respond within the timeout's seconds). */
export type BrokerRequest = {
  readonly method?: string;
  readonly body?: unknown;
  readonly signal?: AbortSignal;
  readonly timeoutMs?: number;
  readonly timeoutMessage?: string;
};

// Discovery and source checks call public providers server-side; every request is bounded.
const REQUEST_TIMEOUT_MS = 90_000;

/**
 * Call a broker JSON route with the demo session and return its parsed reply for the caller to validate.
 * A timeout rejects with a readable message; an aborted caller signal rejects with its AbortError.
 */
export async function brokerJson(path: string, request: BrokerRequest = {}): Promise<unknown> {
  const { body, signal } = request;
  const timeoutMs = request.timeoutMs ?? REQUEST_TIMEOUT_MS;
  const timeout = AbortSignal.timeout(timeoutMs);
  try {
    const response = await brokerFetch(path, {
      method: request.method ?? (body === undefined ? "GET" : "POST"),
      body: body === undefined ? undefined : JSON.stringify(body),
      signal: signal ? anySignal([signal, timeout]) : timeout,
    });
    return await response.json();
  } catch (error) {
    if (timeout.aborted && !signal?.aborted) {
      const seconds = Math.max(1, Math.round(timeoutMs / 1000));
      throw new BrokerError(request.timeoutMessage ??
        `The simulation service did not respond within ${seconds} second${seconds === 1 ? "" : "s"}. Try again.`,
      408, "REQUEST_TIMEOUT", null);
    }
    throw error;
  }
}
