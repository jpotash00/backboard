/**
 * Offboard web SDK — public surface (§7).
 *
 *   import Offboard from "offboard";
 *   Offboard.init({ publicKey: "pk_..." });
 *   Offboard.showCancelFlow({
 *     userId: "user_123",
 *     onResolved: (outcome) => { /* apply outcome.intervention_id, etc. *\/ },
 *   });
 *
 * The engine does the diagnosis and policy authorizes the intervention server-side;
 * the SDK only renders the conversation and hands you the structured outcome.
 */

import { SessionClient } from "./client.js";
import { CancelFlowModal } from "./ui.js";
import type { InitOptions, ShowCancelFlowOptions } from "./types.js";

/**
 * The engine URL an unconfigured install talks to. Precedence, highest first:
 *   1. `apiBaseUrl` passed to `init()` — per-app, e.g. staging vs prod.
 *   2. `globalThis.__OFFBOARD_API_BASE_URL__` — a host can set this (a bundler define, or a
 *      `<script>window.__OFFBOARD_API_BASE_URL__="…"</script>` before init) to flip
 *      environments without editing call sites.
 *   3. This baked-in default — point it at YOUR hosted engine before publishing to npm.
 * No `/v1` suffix: the engine serves routes at the root. If you host it under a version
 * prefix, include that here AND set the API's `OFFBOARD_ROOT_PATH` to match.
 */
const DEFAULT_API_BASE_URL = "https://api.offboard.dev";

function resolveBaseUrl(explicit?: string): string {
  if (explicit) return explicit;
  const injected = (globalThis as Record<string, unknown>).__OFFBOARD_API_BASE_URL__;
  return typeof injected === "string" && injected ? injected : DEFAULT_API_BASE_URL;
}

let config: Required<InitOptions> | null = null;

export const Offboard = {
  /** Configure the SDK once, near app startup, with your publishable key. */
  init(options: InitOptions): void {
    if (!options?.publicKey) {
      throw new Error("Offboard.init: `publicKey` is required.");
    }
    config = {
      publicKey: options.publicKey,
      apiBaseUrl: resolveBaseUrl(options.apiBaseUrl),
    };
  },

  /** Open the in-flow exit interview. Resolves via the `onResolved` callback. */
  showCancelFlow(options: ShowCancelFlowOptions): void {
    if (!config) {
      throw new Error("Offboard.showCancelFlow: call Offboard.init() first.");
    }
    if (typeof document === "undefined") {
      throw new Error("Offboard.showCancelFlow: requires a browser environment.");
    }
    const client = new SessionClient(config.apiBaseUrl, config.publicKey);
    const modal = new CancelFlowModal(client, options);
    void modal.open();
  },

  /** Test seam / advanced: reset configured state. */
  _reset(): void {
    config = null;
  },
};

export default Offboard;

export { OffboardApiError } from "./client.js";
export type {
  CoverStory,
  InitOptions,
  Intervention,
  Outcome,
  Reason,
  ResolvedOutcome,
  SessionOpenResponse,
  SessionTurnResponse,
  ShowCancelFlowOptions,
  UserContext,
} from "./types.js";
