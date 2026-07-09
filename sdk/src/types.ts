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
  /** Declared decision audit from deterministic policy (never the model): the money at
   * stake + margin spent. Keys include customer_value, save_probability, margin_spent. */
  economics?: Record<string, unknown> | null;
  /** Every option policy considered, each with its cost/expected_value and why it won or
   * was rejected — so an authorized spend is fully traceable. */
  decision_trace?: Array<Record<string, unknown>>;
  /** How far policy will go on this confidence: "defer" (nothing — fall back), "suggest"
   * (recommend the offer for the company/ops to apply), or "act" (auto-apply). */
  mode?: "defer" | "suggest" | "act";
  /** Whether the behavioral data backed the diagnosis (corroborated / contradicted /
   * unverified) and the confidence it was adjusted to. */
  corroboration?: Record<string, unknown> | null;
}

/** An authorized action from the customer's own menu, spelled out so the host can
 * render the offer ("50% off for 3 months") without re-fetching config. */
export interface Intervention {
  id: string;
  type: string;
  description: string;
}

/** The outcome plus the resolved intervention, as handed to `onResolved`. */
export interface ResolvedOutcome extends Outcome {
  /** The authorized offer, or null when policy authorized nothing. */
  intervention: Intervention | null;
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
  /** Product-specific behavioral tells that don't fit the fixed fields, e.g.
   * { seats_used: 7 }. Rendered into the interviewer's context server-side. */
  signals?: Record<string, unknown>;
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
  intervention?: Intervention | null;
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

  /** The interview concluded — the diagnosis (and any authorized offer) is in. Fires once,
   * BEFORE the in-chat offer step. Optional analytics hook; the terminal action the user
   * takes is reported via onAccept / onCancel. */
  onResolved?: (outcome: ResolvedOutcome) => void;

  /** The user ACCEPTED the offer shown in-chat. Apply it yourself — e.g. call Stripe using
   * `outcome.intervention.id`. Offboard never touches your billing; it only tells you what
   * the user agreed to. (Inspect `outcome.mode`: "act" = confident enough to auto-apply,
   * "suggest" = you may want a human to approve before charging.) */
  onAccept?: (outcome: ResolvedOutcome) => void;

  /** The user is LEAVING: they declined the offer, no offer was authorized, or they tapped
   * the escape hatch. `outcome` is null only for the escape hatch before any diagnosis.
   * Complete the cancellation here. */
  onCancel?: (outcome: ResolvedOutcome | null) => void;

  /** Legacy alias fired when the user taps the escape hatch before any diagnosis. Prefer
   * `onCancel` (which is also called with null in that case); kept for back-compat. */
  onJustCancel?: () => void;

  /** Copy overrides for the buttons. */
  justCancelLabel?: string;
  acceptLabel?: string;
  declineLabel?: string;
}
