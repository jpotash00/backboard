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

export class SessionClient {
  constructor(
    private readonly baseUrl: string,
    private readonly publicKey: string,
  ) {}

  async open(user: UserContext): Promise<SessionOpenResponse> {
    return this.post<SessionOpenResponse>("/sessions", user);
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

  private async post<T>(path: string, body: unknown): Promise<T> {
    const res = await fetch(this.baseUrl.replace(/\/$/, "") + path, {
      method: "POST",
      headers: {
        "content-type": "application/json",
        authorization: `Bearer ${this.publicKey}`,
      },
      body: JSON.stringify(body),
    });
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
