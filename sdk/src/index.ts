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

const DEFAULT_API_BASE_URL = "https://api.offboard.dev/v1";

let config: Required<InitOptions> | null = null;

export const Offboard = {
  /** Configure the SDK once, near app startup, with your publishable key. */
  init(options: InitOptions): void {
    if (!options?.publicKey) {
      throw new Error("Offboard.init: `publicKey` is required.");
    }
    config = {
      publicKey: options.publicKey,
      apiBaseUrl: options.apiBaseUrl ?? DEFAULT_API_BASE_URL,
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
