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
  /** Structured handle to apply in your billing system. Since Offboard only picks offers you
   *  already authorized, the matching object usually already exists, so this is typically a
   *  reference to it, e.g. `{ stripe_coupon: "off50_3mo" }`. Empty object when the offer defines
   *  none; Offboard never executes it, it only hands it back so you don't parse `description`. */
  params: Record<string, unknown>;
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

/**
 * Make the widget look like YOUR app. The surface renders in a neutral shadcn/ui (zinc)
 * palette by default and is theme-aware (light/dark) on its own. Use this to match your
 * product's design language.
 *
 * All colours are **raw HSL triples**, the way shadcn/Radix store them — `"222 47% 11%"`,
 * NOT `"#112233"` and NOT `"hsl(222 47% 11%)"`. (`radius` is a normal CSS length.)
 */
export interface OffboardTheme {
  /** Adopt the host page's shadcn/ui CSS variables (`--background`, `--foreground`,
   * `--primary`, `--muted`, `--border`, `--ring`, `--radius`, …) so the widget inherits your
   * palette automatically, light and dark, with zero per-call config. Opt-in because it
   * assumes classic HSL-triple shadcn tokens; leave it off on non-shadcn hosts (or hosts on
   * the newer oklch tokens) and set the tokens below explicitly instead. Any explicit token
   * here still wins over adoption. */
  adoptHostTokens?: boolean;
  /** The accent behind the primary CTA and the send button. Defaults to your primary. This is
   * the one knob most hosts touch — point it at your brand colour and every CTA follows. */
  accent?: string;
  accentForeground?: string;
  /** Individual token overrides. Each wins over both host adoption and the built-in default. */
  background?: string;
  foreground?: string;
  muted?: string;
  mutedForeground?: string;
  border?: string;
  ring?: string;
  primary?: string;
  primaryForeground?: string;
  /** Corner radius, e.g. `"0.5rem"`. Defaults to the host `--radius` (when adopting) or the
   * built-in `0.65rem`. */
  radius?: string;
}

export interface InitOptions {
  /** Your publishable key, `pk_...`. */
  publicKey: string;
  /** Override the engine endpoint. Defaults to the hosted API. */
  apiBaseUrl?: string;
}

export interface ShowCancelFlowOptions {
  /** The cancelling user's id. */
  userId: string;
  /** Optional behavioral context; merged into the session's UserContext. For a key with a
   * signing secret this is ignored in favour of `identityToken` — the browser can't be
   * trusted to price its own save, so pass the economics through the signed token instead. */
  context?: Omit<UserContext, "user_id">;

  /** A token minted by YOUR backend (never in the browser) that vouches for this user's
   * economics — mrr, plan, tenure. Required if your Offboard key is configured with a signing
   * secret; the engine authorizes paid offers off these signed claims, not the raw body. See
   * the identity-verification section of the SDK README. */
  identityToken?: string;

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

  /** Small label above the in-chat offer. Honest by default ("Here's what I can do"); avoid
   * false-scarcity copy — the offer's credibility comes from the diagnosis it's tied to, not
   * from manufactured exclusivity. */
  offerEyebrow?: string;

  /** Make the widget match your product's look. See {@link OffboardTheme}. */
  theme?: OffboardTheme;
}
