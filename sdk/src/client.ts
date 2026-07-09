/**
 * Thin client over the Offboard session API (§4). Speaks the exact contract the
 * FastAPI engine (Milestone 2) will expose:
 *
 *   POST /sessions            -> { session_id, message }
 *   POST /sessions/:id/turn   -> { message, done } | { done: true, outcome }
 */

import type {
  SessionOpenResponse,
  SessionTurnResponse,
  UserContext,
} from "./types.js";

export class OffboardApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
  ) {
    super(message);
    this.name = "OffboardApiError";
  }
}

/** Per-request timeout. A cancel flow can't hang on a stalled network — we abort and let the
 * modal surface a clean error with the escape hatch still live. */
const DEFAULT_TIMEOUT_MS = 20_000;

export class SessionClient {
  constructor(
    private readonly baseUrl: string,
    private readonly publicKey: string,
    private readonly timeoutMs: number = DEFAULT_TIMEOUT_MS,
  ) {}

  async open(
    user: UserContext,
    identityToken?: string,
  ): Promise<SessionOpenResponse> {
    return this.post<SessionOpenResponse>("/sessions", {
      ...user,
      // Signed by the host's backend; the engine trusts these claims over the raw body when
      // the customer has a signing secret. Omitted for dev/unsecured keys.
      ...(identityToken ? { identity_token: identityToken } : {}),
    });
  }

  async turn(
    sessionId: string,
    userMessage: string,
  ): Promise<SessionTurnResponse> {
    return this.post<SessionTurnResponse>(
      `/sessions/${encodeURIComponent(sessionId)}/turn`,
      { user_message: userMessage },
    );
  }

  /** Report what the user did with the offer — the realized-save signal for the flywheel.
   * Fire-and-forget from the modal; failures must never affect the user's experience. */
  async resolution(sessionId: string, accepted: boolean): Promise<void> {
    await this.post<unknown>(
      `/sessions/${encodeURIComponent(sessionId)}/resolution`,
      { accepted },
    );
  }

  private async post<T>(path: string, body: unknown): Promise<T> {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), this.timeoutMs);
    let res: Response;
    try {
      res = await fetch(this.baseUrl.replace(/\/$/, "") + path, {
        method: "POST",
        headers: {
          "content-type": "application/json",
          authorization: `Bearer ${this.publicKey}`,
        },
        body: JSON.stringify(body),
        signal: controller.signal,
      });
    } catch (err) {
      // AbortError (timeout) or a network failure — status 0 tells the caller it never
      // reached the server, so the modal shows the "couldn't reach us" fallback.
      const timedOut = err instanceof DOMException && err.name === "AbortError";
      throw new OffboardApiError(
        timedOut
          ? `Offboard API timed out after ${this.timeoutMs}ms on ${path}`
          : `Offboard API request failed on ${path}`,
        0,
      );
    } finally {
      clearTimeout(timer);
    }
    if (!res.ok) {
      const detail = await res.text().catch(() => "");
      throw new OffboardApiError(
        `Offboard API ${res.status} on ${path}: ${detail || res.statusText}`,
        res.status,
      );
    }
    return (await res.json()) as T;
  }
}
