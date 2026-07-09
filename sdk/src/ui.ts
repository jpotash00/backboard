/**
 * The modal chat. We own this UI on purpose (§7): if the host built their own they
 * could bury the escape hatch or degrade the conversation, and we couldn't fix it
 * without their redeploy.
 *
 * Two hard constraints are enforced structurally here, not by convention:
 *   #3 The "Just cancel" escape hatch is ALWAYS rendered and always one tap away.
 *   #1 The conversation is bounded — the engine stops at MAX_TURNS; the UI just
 *      renders whatever the server returns and closes on `done`.
 */

import { OffboardApiError, SessionClient } from "./client.js";
import type { ResolvedOutcome, ShowCancelFlowOptions, UserContext } from "./types.js";

const STYLE_ID = "offboard-styles";

const CSS = `
.offboard-overlay{position:fixed;inset:0;background:rgba(15,17,21,.55);
  display:flex;align-items:flex-end;justify-content:center;z-index:2147483647;
  font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif}
@media(min-width:640px){.offboard-overlay{align-items:center}}
.offboard-modal{background:#fff;width:100%;max-width:440px;border-radius:16px 16px 0 0;
  box-shadow:0 -8px 40px rgba(0,0,0,.2);display:flex;flex-direction:column;
  max-height:80vh;overflow:hidden}
@media(min-width:640px){.offboard-modal{border-radius:16px}}
.offboard-log{padding:20px;overflow-y:auto;display:flex;flex-direction:column;gap:12px;flex:1}
.offboard-msg{max-width:85%;padding:10px 14px;border-radius:14px;line-height:1.4;font-size:15px}
.offboard-msg.bot{align-self:flex-start;background:#f1f3f5;color:#1a1d21;border-bottom-left-radius:4px}
.offboard-msg.user{align-self:flex-end;background:#2f6fed;color:#fff;border-bottom-right-radius:4px}
.offboard-input-row{display:flex;gap:8px;padding:12px 16px;border-top:1px solid #eceef0}
.offboard-input{flex:1;border:1px solid #d5d9de;border-radius:10px;padding:10px 12px;
  font-size:15px;outline:none}
.offboard-input:focus{border-color:#2f6fed}
.offboard-send{border:0;background:#2f6fed;color:#fff;border-radius:10px;padding:0 16px;
  font-size:15px;font-weight:600;cursor:pointer}
.offboard-send:disabled{opacity:.5;cursor:default}
.offboard-escape{background:transparent;border:0;color:#8a9099;font-size:13px;
  padding:10px;cursor:pointer;text-align:center;width:100%}
.offboard-escape:hover{color:#2f6fed;text-decoration:underline}
.offboard-msg.offer{align-self:stretch;max-width:100%;background:#eef4ff;
  border:1px solid #d3e0ff;color:#12203a;font-weight:500}
.offboard-actions{display:flex;gap:8px;padding:12px 16px;border-top:1px solid #eceef0}
.offboard-accept{flex:1;border:0;background:#2ea34d;color:#fff;border-radius:10px;
  padding:11px;font-size:15px;font-weight:600;cursor:pointer}
.offboard-decline{border:1px solid #d5d9de;background:#fff;color:#555;border-radius:10px;
  padding:11px 16px;font-size:15px;cursor:pointer}
`;

function ensureStyles(): void {
  if (document.getElementById(STYLE_ID)) return;
  const el = document.createElement("style");
  el.id = STYLE_ID;
  el.textContent = CSS;
  document.head.appendChild(el);
}

export class CancelFlowModal {
  private readonly overlay: HTMLDivElement;
  private readonly modal: HTMLDivElement;
  private readonly log: HTMLDivElement;
  private readonly inputRow: HTMLDivElement;
  private readonly input: HTMLInputElement;
  private readonly send: HTMLButtonElement;
  private readonly escapeBtn: HTMLButtonElement;
  private sessionId: string | null = null;
  private closed = false;
  private settled = false; // an offer decision (accept/cancel) has been reported
  private pendingOutcome: ResolvedOutcome | null = null; // set once an offer is on screen

  constructor(
    private readonly client: SessionClient,
    private readonly opts: ShowCancelFlowOptions,
  ) {
    ensureStyles();

    this.overlay = document.createElement("div");
    this.overlay.className = "offboard-overlay";
    this.overlay.setAttribute("role", "dialog");
    this.overlay.setAttribute("aria-modal", "true");

    this.modal = document.createElement("div");
    this.modal.className = "offboard-modal";

    this.log = document.createElement("div");
    this.log.className = "offboard-log";

    this.inputRow = document.createElement("div");
    this.inputRow.className = "offboard-input-row";
    this.input = document.createElement("input");
    this.input.className = "offboard-input";
    this.input.placeholder = "Type your reply…";
    this.input.autocomplete = "off";
    this.send = document.createElement("button");
    this.send.className = "offboard-send";
    this.send.textContent = "Send";
    this.inputRow.append(this.input, this.send);

    // Hard constraint #3: the escape hatch is always here.
    this.escapeBtn = document.createElement("button");
    this.escapeBtn.className = "offboard-escape";
    this.escapeBtn.textContent = opts.justCancelLabel ?? "Just cancel";
    // Before a diagnosis this reports null; once an offer is on screen it reports the outcome.
    this.escapeBtn.addEventListener("click", () => this.leave(this.pendingOutcome));

    this.modal.append(this.log, this.inputRow, this.escapeBtn);
    this.overlay.append(this.modal);

    this.send.addEventListener("click", () => void this.submit());
    this.input.addEventListener("keydown", (e) => {
      if (e.key === "Enter") void this.submit();
    });
  }

  async open(): Promise<void> {
    document.body.appendChild(this.overlay);
    this.input.focus();
    const user: UserContext = { user_id: this.opts.userId, ...this.opts.context };
    try {
      const res = await this.client.open(user);
      this.sessionId = res.session_id;
      this.appendBot(res.message);
    } catch (err) {
      this.fail(err);
    }
  }

  private async submit(): Promise<void> {
    const text = this.input.value.trim();
    if (!text || !this.sessionId || this.closed) return;
    this.appendUser(text);
    this.input.value = "";
    this.setBusy(true);
    try {
      const res = await this.client.turn(this.sessionId, text);
      if (res.message) this.appendBot(res.message);
      if (res.done && res.outcome) {
        const outcome: ResolvedOutcome = {
          ...res.outcome,
          intervention: res.intervention ?? null,
        };
        // Diagnosis is in (analytics hook), before the user chooses on the offer.
        this.opts.onResolved?.(outcome);
        if (outcome.intervention && outcome.mode !== "defer") {
          this.presentOffer(outcome);      // show the offer in-chat, await yes/no
        } else {
          this.leave(outcome);             // nothing authorized -> they're leaving
        }
        return;
      }
    } catch (err) {
      this.fail(err);
      return;
    }
    this.setBusy(false);
    this.input.focus();
  }

  /** Show the authorized offer in the chat with Accept / No-thanks. We only present it and
   * report the choice — applying it (Stripe, etc.) is the host's job via onAccept. */
  private presentOffer(outcome: ResolvedOutcome): void {
    const offer = outcome.intervention;
    if (!offer) return this.leave(outcome);

    this.pendingOutcome = outcome;   // the escape hatch now reports this outcome, not null
    this.appendMsg(offer.description, "offer");
    this.inputRow.remove();               // the conversation is over; it's a yes/no now

    const actions = document.createElement("div");
    actions.className = "offboard-actions";
    const accept = document.createElement("button");
    accept.className = "offboard-accept";
    accept.textContent = this.opts.acceptLabel ?? "Accept offer";
    accept.addEventListener("click", () => this.accept(outcome));
    const decline = document.createElement("button");
    decline.className = "offboard-decline";
    decline.textContent = this.opts.declineLabel ?? "No thanks";
    decline.addEventListener("click", () => this.leave(outcome));
    actions.append(accept, decline);
    this.modal.insertBefore(actions, this.escapeBtn);
    accept.focus();
  }

  private accept(outcome: ResolvedOutcome): void {
    if (this.settled) return;
    this.settled = true;
    this.close();
    this.opts.onAccept?.(outcome);        // host applies the offer (e.g. calls Stripe)
  }

  private leave(outcome: ResolvedOutcome | null): void {
    if (this.settled) return;
    this.settled = true;
    this.close();
    this.opts.onCancel?.(outcome);        // declined / no offer / escape hatch -> host cancels
  }

  private fail(err: unknown): void {
    // On any transport failure we do NOT trap the user — we let them cancel cleanly.
    const msg =
      err instanceof OffboardApiError
        ? "Sorry, something went wrong on our end."
        : "Sorry, we couldn't reach the server.";
    this.appendBot(msg);
    this.setBusy(true);
  }

  private appendBot(text: string): void {
    this.appendMsg(text, "bot");
  }

  private appendUser(text: string): void {
    this.appendMsg(text, "user");
  }

  private appendMsg(text: string, who: "bot" | "user" | "offer"): void {
    const el = document.createElement("div");
    el.className = `offboard-msg ${who}`;
    el.textContent = text;
    this.log.appendChild(el);
    this.log.scrollTop = this.log.scrollHeight;
  }

  private setBusy(busy: boolean): void {
    this.send.disabled = busy;
    this.input.disabled = busy;
  }

  private close(): void {
    if (this.closed) return;
    this.closed = true;
    this.overlay.remove();
  }
}
