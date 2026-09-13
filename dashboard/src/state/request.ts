import { recordRequest } from "./request-health";

export class HttpError extends Error {
  constructor(public status: number, message: string) { super(message); }
}

export interface RequestPolicy { timeoutMs?: number; retries?: number; retryDelayMs?: number }

function timeoutError(method: string, url: string): DOMException {
  const mutationNote = method === "GET"
    ? ""
    : "; the request outcome is unknown and it may have completed. Refresh status before retrying.";
  return new DOMException(`${method} ${url} timed out${mutationNote}`, "TimeoutError");
}

type Decoder<T> = (response: Response) => Promise<T>;

/** Share a recurring request while it is active. Unlike cancellation-based
 * polling this lets a slow but healthy response finish and clears the guard
 * on either success or failure. */
export function createInFlightGuard<T>(work: () => Promise<T>): () => Promise<T> {
  let inFlight: Promise<T> | null = null;
  return () => {
    if (inFlight) return inFlight;
    let request: Promise<T>;
    try {
      request = work();
    } catch (error) {
      request = Promise.reject(error);
    }
    inFlight = request;
    void request.then(
      () => { if (inFlight === request) inFlight = null; },
      () => { if (inFlight === request) inFlight = null; },
    );
    return request;
  };
}

/** Deadline includes reading/parsing the body, not just receiving headers.
 * Mutations are attempted exactly once: a timeout does not prove they failed.
 */
async function request<T>(url: string, init: RequestInit | undefined, policy: RequestPolicy, decode: Decoder<T>): Promise<T> {
  const method = (init?.method ?? "GET").toUpperCase();
  const retries = method === "GET" ? Math.min(2, policy.retries ?? 2) : 0;
  const headers = new Headers(init?.headers);
  headers.set("X-The-Lab-Source", "dashboard");
  for (let attempt = 0; ; attempt++) {
    const controller = new AbortController();
    const callerSignal = init?.signal;
    let timer: ReturnType<typeof setTimeout> | undefined;
    let onAbort: (() => void) | undefined;
    try {
      const cancelled = new Promise<never>((_, reject) => {
        onAbort = () => {
          controller.abort();
          reject(callerSignal?.reason ?? new DOMException("Cancelled", "AbortError"));
        };
        callerSignal?.addEventListener("abort", onAbort, { once: true });
        if (callerSignal?.aborted) onAbort();
        timer = setTimeout(() => {
          controller.abort();
          reject(timeoutError(method, url));
        }, policy.timeoutMs ?? 15_000);
      });
      const response = (async () => {
        if (callerSignal?.aborted) throw callerSignal.reason;
        const res = await fetch(url, { ...init, headers, signal: controller.signal });
        if (!res.ok) throw new HttpError(res.status, `${method} ${url} returned ${res.status}`);
        return decode(res);
      })();
      const data = await Promise.race([response, cancelled]);
      recordRequest(url);
      return data;
    } catch (error) {
      if (callerSignal?.aborted) throw error;
      recordRequest(url, error);
      const retryable = error instanceof HttpError
        ? [408, 429, 500, 502, 503, 504].includes(error.status)
        : error instanceof TypeError || (error instanceof DOMException && ["TimeoutError", "AbortError"].includes(error.name));
      if (attempt >= retries || !retryable) throw error;
    } finally {
      clearTimeout(timer);
      if (onAbort) callerSignal?.removeEventListener("abort", onAbort);
    }
    await new Promise(resolve => setTimeout(resolve, (policy.retryDelayMs ?? 300) * 2 ** attempt));
    if (init?.signal?.aborted) throw init.signal.reason;
  }
}

export function requestJson<T>(url: string, init?: RequestInit, policy: RequestPolicy = {}): Promise<T> {
  return request(url, init, policy, (response) => response.json() as Promise<T>);
}

/** JSON is the common API shape, but a few bounded polling endpoints return
 * plain text (agent output). Keep their cancellation/retry semantics aligned.
 */
export function requestText(url: string, init?: RequestInit, policy: RequestPolicy = {}): Promise<string> {
  return request(url, init, policy, (response) => response.text());
}
