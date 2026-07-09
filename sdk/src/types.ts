/**
 * The wire contract, mirroring engine/taxonomy.py and §4 of the build brief.
 * This is the single source of truth for the shape that crosses the network —
 * keep it in lockstep with the Python `Outcome` dataclass.
 */

export type Reason =
  | "never_activated"
  | "value_ended"
  | "price_value_mismatch"
  | "missing_capability"
  | "switched_competitor"
  | "product_quality"
  | "involuntary"
  | "unknown";

export type CoverStory =
  | "too_expensive"
  | "not_using_it"
  | "found_alternative"
  | "missing_feature"
  | "too_complicated"
  | "no_reason_given";

/**
 * The product. `cover_story` vs `reason` is the entire pitch in one field:
 * "they said price, they meant activation."
 */
export interface Outcome {
  reason: Reason;
  confidence: number;
  evidence: string;
  cover_story: string;
  savable: boolean;
  intervention_id: string | null;
  rationale: string;
  turns_used: number;
}

/**
 * Behavioral signal the host app supplies — this is what disambiguates the cover
 * story (hard constraint #6). Mirrors the POST /sessions body in §4. Only `user_id`
 * is strictly required; the richer the context, the sharper the diagnosis.
 */
export interface UserContext {
  user_id: string;
  plan?: string;
  mrr?: number;
  tenure_days?: number;
  logins_last_30d?: number;
  activated?: boolean;
  usage_summary?: string;
}

// --- Session API responses (§4) ---

export interface SessionOpenResponse {
  session_id: string;
  message: string;
}

export interface SessionTurnResponse {
  message?: string;
  done: boolean;
  outcome?: Outcome;
}

// --- SDK options ---

export interface InitOptions {
  /** Your publishable key, `pk_...`. */
  publicKey: string;
  /** Override the engine endpoint. Defaults to the hosted API. */
  apiBaseUrl?: string;
}

export interface ShowCancelFlowOptions {
  /** The cancelling user's id. */
  userId: string;
  /** Optional behavioral context; merged into the session's UserContext. */
  context?: Omit<UserContext, "user_id">;
  /** Called once the interview resolves to a structured outcome. */
  onResolved: (outcome: Outcome) => void;
  /** Called when the user taps the always-visible "Just cancel" escape hatch. */
  onJustCancel?: () => void;
  /** Optional copy override for the escape-hatch button. */
  justCancelLabel?: string;
}
