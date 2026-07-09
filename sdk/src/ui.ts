/**
 * The modal chat. We own this UI on purpose (§7): if the host built their own they
 * could bury the escape hatch or degrade the conversation, and we couldn't fix it
 * without their redeploy.
 *
 * Two hard constraints are enforced structurally here, not by convention:
 *   #3 The "Just cancel" escape hatch is ALWAYS rendered and always one tap away.
 *   #1 The conversation is bounded — the engine stops at MAX_TURNS; the UI just
 *      renders whatever the server returns and closes on `done`.
 *
 * Design intent: a calm, respectful surface. Someone is leaving — nothing here
 * should feel like a trap. Apple-grade restraint: layered material, a single
 * accent, spring motion, honest typography, and an exit that stays dignified.
 */

import { OffboardApiError, SessionClient } from "./client.js";
import type { ResolvedOutcome, ShowCancelFlowOptions, UserContext } from "./types.js";

const STYLE_ID = "offboard-styles";

/**
 * All styles are scoped under `.offboard-overlay` and driven by CSS variables so
 * light/dark are one source of truth. The design tokens mirror Apple's system
 * palette (systemGray6 surfaces, SF label inks, the system blue accent).
 */
const CSS = `
.offboard-overlay,.offboard-overlay *{ box-sizing:border-box }
.offboard-overlay{
  --ob-ink:#1d1d1f; --ob-ink-dim:#86868b; --ob-surface:#ffffff;
  --ob-surface-2:#f2f2f7; --ob-hairline:rgba(0,0,0,.08);
  --ob-accent:#0071e3; --ob-accent-strong:#0064cf; --ob-on-accent:#ffffff;
  --ob-good:#1a8a4a; --ob-good-soft:rgba(26,138,74,.10);
  --ob-shadow:0 12px 28px rgba(0,0,0,.12), 0 40px 80px rgba(0,0,0,.24);
  --ob-ease:cubic-bezier(.32,.72,0,1);
  position:fixed; inset:0; z-index:2147483647;
  display:flex; align-items:flex-end; justify-content:center;
  padding:0; background:rgba(0,0,0,.30);
  -webkit-backdrop-filter:blur(20px) saturate(160%); backdrop-filter:blur(20px) saturate(160%);
  opacity:0; animation:ob-fade .32s var(--ob-ease) forwards;
  font-family:-apple-system,BlinkMacSystemFont,"SF Pro Text","Segoe UI",Roboto,sans-serif;
  -webkit-font-smoothing:antialiased; text-rendering:optimizeLegibility;
}
@media (prefers-color-scheme: dark){
  .offboard-overlay{
    --ob-ink:#f5f5f7; --ob-ink-dim:#98989d; --ob-surface:#1c1c1e;
    --ob-surface-2:#2c2c2e; --ob-hairline:rgba(255,255,255,.10);
    --ob-accent:#0a84ff; --ob-accent-strong:#0a84ff; --ob-on-accent:#ffffff;
    --ob-good:#30d158; --ob-good-soft:rgba(48,209,88,.14);
    --ob-shadow:0 12px 28px rgba(0,0,0,.44), 0 40px 90px rgba(0,0,0,.60);
    background:rgba(0,0,0,.48);
  }
}
@media (min-width:640px){ .offboard-overlay{ align-items:center; padding:24px } }

.offboard-modal{
  position:relative; display:flex; flex-direction:column;
  width:100%; max-width:420px; max-height:min(88vh,720px);
  background:var(--ob-surface); color:var(--ob-ink);
  border-radius:28px 28px 0 0; box-shadow:var(--ob-shadow); overflow:hidden;
  transform:translateY(24px); opacity:0;
  animation:ob-rise .5s var(--ob-ease) .02s forwards;
}
@media (min-width:640px){
  .offboard-modal{ border-radius:24px; transform:translateY(10px) scale(.98) }
}

/* Grabber — the affordance that says "this is a sheet you own". */
.offboard-grabber{ display:flex; justify-content:center; padding:10px 0 2px; flex:0 0 auto }
.offboard-grabber::before{ content:""; width:36px; height:5px; border-radius:3px;
  background:var(--ob-ink); opacity:.16 }
@media (min-width:640px){ .offboard-grabber{ display:none } }

/* Header — a quiet identity so the conversation feels attended, not automated. */
.offboard-head{ display:flex; align-items:center; gap:12px; padding:14px 20px 12px; flex:0 0 auto }
.offboard-orb{ width:34px; height:34px; border-radius:50%; flex:0 0 auto; position:relative;
  background:radial-gradient(120% 120% at 30% 20%, #7db8ff 0%, var(--ob-accent) 46%, #7a5cff 100%);
  box-shadow:0 2px 8px rgba(0,113,227,.30) }
.offboard-orb::after{ content:""; position:absolute; inset:0; border-radius:50%;
  box-shadow:inset 0 1px 1px rgba(255,255,255,.5) }
.offboard-head-text{ display:flex; flex-direction:column; min-width:0; gap:1px }
.offboard-title{ font-size:15px; font-weight:600; letter-spacing:-.01em; line-height:1.2 }
.offboard-status{ font-size:12.5px; color:var(--ob-ink-dim); line-height:1.3; height:16px;
  display:flex; align-items:center; gap:6px }
.offboard-status .dot{ width:6px; height:6px; border-radius:50%; background:var(--ob-good);
  box-shadow:0 0 0 0 var(--ob-good-soft); animation:ob-pulse 2.4s ease-in-out infinite }

/* Message log */
.offboard-log{ flex:1 1 auto; min-width:0; overflow-y:auto; -webkit-overflow-scrolling:touch;
  display:flex; flex-direction:column; gap:8px; padding:8px 20px 4px;
  scrollbar-width:thin; scrollbar-color:var(--ob-hairline) transparent }
.offboard-log::-webkit-scrollbar{ width:8px }
.offboard-log::-webkit-scrollbar-thumb{ background:var(--ob-hairline); border-radius:8px }

.offboard-msg{ max-width:82%; padding:10px 14px; font-size:15px; line-height:1.45;
  letter-spacing:-.006em; border-radius:20px; word-wrap:break-word;
  animation:ob-pop .42s var(--ob-ease) both }
.offboard-msg.bot{ align-self:flex-start; background:var(--ob-surface-2); color:var(--ob-ink);
  border-bottom-left-radius:7px }
.offboard-msg.user{ align-self:flex-end; color:var(--ob-on-accent); border-bottom-right-radius:7px;
  background:linear-gradient(180deg,var(--ob-accent) 0%,var(--ob-accent-strong) 100%);
  box-shadow:0 1px 2px rgba(0,113,227,.28) }

/* Typing indicator — three-dot bubble while the engine thinks. */
.offboard-typing{ align-self:flex-start; display:flex; gap:5px; align-items:center;
  padding:13px 16px; background:var(--ob-surface-2); border-radius:20px;
  border-bottom-left-radius:7px; animation:ob-pop .3s var(--ob-ease) both }
.offboard-typing span{ width:7px; height:7px; border-radius:50%; background:var(--ob-ink);
  opacity:.28; animation:ob-blink 1.3s ease-in-out infinite }
.offboard-typing span:nth-child(2){ animation-delay:.18s }
.offboard-typing span:nth-child(3){ animation-delay:.36s }

/* Composer — a pill field with a circular send, iMessage-grade. */
.offboard-foot{ flex:0 0 auto; padding:12px 16px calc(12px + env(safe-area-inset-bottom,0px));
  border-top:1px solid var(--ob-hairline); background:var(--ob-surface) }
.offboard-input-row{ display:flex; align-items:flex-end; gap:8px }
.offboard-input{ flex:1; min-height:42px; border:1px solid var(--ob-hairline);
  background:var(--ob-surface-2); color:var(--ob-ink); border-radius:21px;
  padding:10px 16px; font:inherit; font-size:15px; line-height:1.35; outline:none;
  transition:border-color .18s ease, box-shadow .18s ease }
.offboard-input::placeholder{ color:var(--ob-ink-dim) }
.offboard-input:focus{ border-color:var(--ob-accent);
  box-shadow:0 0 0 4px color-mix(in srgb,var(--ob-accent) 16%,transparent) }
.offboard-send{ flex:0 0 auto; width:42px; height:42px; border:0; border-radius:50%; cursor:pointer;
  background:var(--ob-accent); color:var(--ob-on-accent); display:grid; place-items:center;
  transition:transform .16s var(--ob-ease), opacity .16s ease, background .16s ease }
.offboard-send svg{ width:20px; height:20px }
.offboard-send:not(:disabled):hover{ background:var(--ob-accent-strong) }
.offboard-send:not(:disabled):active{ transform:scale(.90) }
.offboard-send:disabled{ opacity:.35; cursor:default }

/* The escape hatch — hard constraint #3. Understated, never buried, always one tap. */
.offboard-escape{ display:block; width:100%; margin-top:6px; padding:9px; border:0;
  background:transparent; color:var(--ob-ink-dim); font:inherit; font-size:13px; cursor:pointer;
  border-radius:10px; transition:color .16s ease, background .16s ease }
.offboard-escape:hover{ color:var(--ob-ink); background:var(--ob-surface-2) }

/* In-chat offer card — the personalized "one reason to stay", presented calmly. */
.offboard-offer{ align-self:stretch; max-width:100%; margin:6px 0 2px; border-radius:20px;
  border:1px solid var(--ob-hairline); background:var(--ob-surface);
  box-shadow:0 8px 22px rgba(0,0,0,.08); overflow:hidden;
  animation:ob-pop .5s var(--ob-ease) both }
@media (prefers-color-scheme: dark){ .offboard-offer{ background:var(--ob-surface-2) } }
.offboard-offer-top{ padding:16px 18px 14px;
  background:linear-gradient(180deg,color-mix(in srgb,var(--ob-accent) 8%,transparent),transparent) }
.offboard-offer-eyebrow{ display:flex; align-items:center; gap:7px; font-size:12px; font-weight:600;
  letter-spacing:.02em; text-transform:uppercase; color:var(--ob-accent) }
.offboard-offer-eyebrow svg{ width:14px; height:14px }
.offboard-offer-headline{ margin-top:8px; font-size:19px; font-weight:600; letter-spacing:-.02em;
  line-height:1.28; color:var(--ob-ink) }
.offboard-offer-sub{ margin-top:6px; font-size:13.5px; line-height:1.5; color:var(--ob-ink-dim) }
.offboard-offer-actions{ display:flex; flex-direction:column; gap:8px; padding:4px 14px 14px }
.offboard-btn{ width:100%; padding:13px 16px; border-radius:14px; font:inherit; font-size:15px;
  font-weight:600; letter-spacing:-.01em; cursor:pointer; border:1px solid transparent;
  transition:transform .16s var(--ob-ease), background .16s ease, opacity .16s ease }
.offboard-btn:active{ transform:scale(.98) }
.offboard-btn-primary{ background:var(--ob-accent); color:var(--ob-on-accent);
  box-shadow:0 4px 14px rgba(0,113,227,.30) }
.offboard-btn-primary:hover{ background:var(--ob-accent-strong) }
.offboard-btn-ghost{ background:transparent; color:var(--ob-ink-dim); border-color:var(--ob-hairline) }
.offboard-btn-ghost:hover{ color:var(--ob-ink); background:var(--ob-surface-2) }

/* Terminal confirmation — a graceful close, whether they stayed or left. */
.offboard-final{ align-self:stretch; max-width:100%; text-align:center; margin:auto 0;
  padding:40px 20px 44px; animation:ob-pop .45s var(--ob-ease) both }
.offboard-final-badge{ width:56px; height:56px; margin:0 auto 14px; border-radius:50%;
  display:grid; place-items:center; background:var(--ob-good-soft); color:var(--ob-good) }
.offboard-final-badge svg{ width:28px; height:28px }
.offboard-final-badge.neutral{ background:var(--ob-surface-2); color:var(--ob-ink-dim) }
.offboard-final-title{ font-size:18px; font-weight:600; letter-spacing:-.015em; color:var(--ob-ink) }
.offboard-final-sub{ margin-top:6px; font-size:14px; line-height:1.5; color:var(--ob-ink-dim) }

@keyframes ob-fade{ to{ opacity:1 } }
@keyframes ob-rise{ to{ transform:none; opacity:1 } }
@keyframes ob-pop{ from{ opacity:0; transform:translateY(8px) scale(.98) } to{ opacity:1; transform:none } }
@keyframes ob-blink{ 0%,60%,100%{ opacity:.28; transform:translateY(0) }
  30%{ opacity:.85; transform:translateY(-2px) } }
@keyframes ob-pulse{ 0%,100%{ box-shadow:0 0 0 0 var(--ob-good-soft) }
  50%{ box-shadow:0 0 0 5px transparent } }

@media (prefers-reduced-motion: reduce){
  .offboard-overlay,.offboard-modal,.offboard-msg,.offboard-typing,.offboard-offer,
  .offboard-final{ animation-duration:.001s }
  .offboard-typing span,.offboard-status .dot{ animation:none }
}
`;

const SEND_ICON =
  '<svg viewBox="0 0 24 24" fill="none" aria-hidden="true">' +
  '<path d="M12 19V5M12 5l-6 6M12 5l6 6" stroke="currentColor" ' +
  'stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"/></svg>';

const SPARK_ICON =
  '<svg viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">' +
  '<path d="M12 2l1.9 5.6a3 3 0 0 0 2.5 2L22 12l-5.6 1.9a3 3 0 0 0-2 2.5L12 22l-1.9-5.6a3 3 0 0 0-2.5-2' +
  'L2 12l5.6-1.9a3 3 0 0 0 2-2.5L12 2z"/></svg>';

const CHECK_ICON =
  '<svg viewBox="0 0 24 24" fill="none" aria-hidden="true">' +
  '<path d="M20 6L9 17l-5-5" stroke="currentColor" stroke-width="2.6" ' +
  'stroke-linecap="round" stroke-linejoin="round"/></svg>';

const WAVE_ICON =
  '<svg viewBox="0 0 24 24" fill="none" aria-hidden="true">' +
  '<path d="M18 8a6 6 0 1 0-12 0v3a6 6 0 0 0 6 6M12 22a6 6 0 0 0 6-6M6 11l-2 2M18 11l2 2" ' +
  'stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg>';

function ensureStyles(): void {
  if (document.getElementById(STYLE_ID)) return;
  const el = document.createElement("style");
  el.id = STYLE_ID;
  el.textContent = CSS;
  document.head.appendChild(el);
}

function el<K extends keyof HTMLElementTagNameMap>(
  tag: K,
  className?: string,
): HTMLElementTagNameMap[K] {
  const node = document.createElement(tag);
  if (className) node.className = className;
  return node;
}

export class CancelFlowModal {
  private readonly overlay: HTMLDivElement;
  private readonly log: HTMLDivElement;
  private readonly foot: HTMLDivElement;
  private readonly input: HTMLInputElement;
  private readonly send: HTMLButtonElement;
  private readonly status: HTMLDivElement;
  private typingEl: HTMLDivElement | null = null;
  private sessionId: string | null = null;
  private closed = false;

  constructor(
    private readonly client: SessionClient,
    private readonly opts: ShowCancelFlowOptions,
  ) {
    ensureStyles();

    this.overlay = el("div", "offboard-overlay");
    this.overlay.setAttribute("role", "dialog");
    this.overlay.setAttribute("aria-modal", "true");
    this.overlay.setAttribute("aria-label", "Cancel subscription");

    const modal = el("div", "offboard-modal");
    modal.append(el("div", "offboard-grabber"));

    // Header identity — a quiet signal that a person (not a wall) is listening.
    const head = el("div", "offboard-head");
    const headText = el("div", "offboard-head-text");
    const title = el("div", "offboard-title");
    title.textContent = "Before you go";
    this.status = el("div", "offboard-status");
    headText.append(title, this.status);
    head.append(el("div", "offboard-orb"), headText);

    this.log = el("div", "offboard-log");

    // Composer.
    this.foot = el("div", "offboard-foot");
    const inputRow = el("div", "offboard-input-row");
    this.input = el("input", "offboard-input");
    this.input.type = "text";
    this.input.placeholder = "Type your reply…";
    this.input.autocomplete = "off";
    this.input.setAttribute("aria-label", "Your reply");
    this.send = el("button", "offboard-send");
    this.send.type = "button";
    this.send.innerHTML = SEND_ICON;
    this.send.setAttribute("aria-label", "Send");
    this.send.disabled = true;
    inputRow.append(this.input, this.send);

    // Hard constraint #3: the escape hatch is always here, one tap away.
    const escape = el("button", "offboard-escape");
    escape.type = "button";
    escape.textContent = this.opts.justCancelLabel ?? "Just cancel my subscription";
    escape.addEventListener("click", () => this.leaveNow(null));

    this.foot.append(inputRow, escape);
    modal.append(head, this.log, this.foot);
    this.overlay.append(modal);

    this.send.addEventListener("click", () => void this.submit());
    this.input.addEventListener("input", () => this.syncSend());
    this.input.addEventListener("keydown", (e) => {
      if (e.key === "Enter" && !e.shiftKey) {
        e.preventDefault();
        void this.submit();
      }
    });
  }

  async open(): Promise<void> {
    document.body.appendChild(this.overlay);
    this.input.focus();
    this.setStatus("connecting…");
    this.showTyping();
    const user: UserContext = { user_id: this.opts.userId, ...this.opts.context };
    try {
      const res = await this.client.open(user);
      this.sessionId = res.session_id;
      this.hideTyping();
      this.setStatus("online");
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
    this.syncSend();
    this.setBusy(true);
    this.showTyping();
    try {
      const res = await this.client.turn(this.sessionId, text);
      this.hideTyping();
      if (res.message) this.appendBot(res.message);
      if (res.done) {
        this.conclude({
          ...(res.outcome as ResolvedOutcome),
          intervention: res.intervention ?? null,
        });
        return;
      }
    } catch (err) {
      this.fail(err);
      return;
    }
    this.setBusy(false);
    this.setStatus("online");
    this.input.focus();
  }

  /**
   * The diagnosis is in. We keep the user in this one calm surface rather than
   * bouncing them back to the host page: present the authorized offer (or a
   * graceful farewell) in-chat, and only then fire the terminal callback.
   */
  private conclude(outcome: ResolvedOutcome): void {
    this.opts.onResolved?.(outcome);
    this.foot.style.display = "none";
    this.setStatus("");
    if (outcome.intervention) {
      this.renderOffer(outcome);
    } else {
      this.renderFarewell(outcome);
    }
  }

  private renderOffer(outcome: ResolvedOutcome): void {
    const intervention = outcome.intervention!;
    const card = el("div", "offboard-offer");

    const top = el("div", "offboard-offer-top");
    const eyebrow = el("div", "offboard-offer-eyebrow");
    eyebrow.innerHTML = SPARK_ICON + "<span>Just for you</span>";
    const headline = el("div", "offboard-offer-headline");
    headline.textContent = intervention.description;
    top.append(eyebrow, headline);
    if (outcome.rationale) {
      const sub = el("div", "offboard-offer-sub");
      sub.textContent = outcome.rationale;
      top.append(sub);
    }

    const actions = el("div", "offboard-offer-actions");
    const accept = el("button", "offboard-btn offboard-btn-primary");
    accept.type = "button";
    accept.textContent = this.opts.acceptLabel ?? "Keep my subscription";
    accept.addEventListener("click", () => this.acceptOffer(outcome));
    const decline = el("button", "offboard-btn offboard-btn-ghost");
    decline.type = "button";
    decline.textContent = this.opts.declineLabel ?? "No thanks, cancel";
    decline.addEventListener("click", () => this.leaveNow(outcome));
    actions.append(accept, decline);

    card.append(top, actions);
    this.log.appendChild(card);
    this.scrollToEnd();
  }

  private acceptOffer(outcome: ResolvedOutcome): void {
    this.reportResolution(true); // realized save — feeds the flywheel
    this.renderFinal(
      "good",
      CHECK_ICON,
      "You're all set",
      "Your subscription is staying active. We're glad you're here.",
    );
    this.finish(() => this.opts.onAccept?.(outcome));
  }

  private renderFarewell(outcome: ResolvedOutcome): void {
    // No offer was authorized — we don't stall the user; we let them leave cleanly.
    this.reportResolution(false);
    this.renderFinal(
      "neutral",
      WAVE_ICON,
      "Your cancellation is confirmed",
      "Thanks for giving us a try. You can come back anytime.",
    );
    this.finish(() => this.opts.onCancel?.(outcome));
  }

  /** Tell the engine what the user did with the offer. Fire-and-forget: a logging failure
   * must never affect the flow, so we swallow errors. */
  private reportResolution(accepted: boolean): void {
    if (!this.sessionId) return;
    void this.client.resolution(this.sessionId, accepted).catch(() => {});
  }

  private renderFinal(
    tone: "good" | "neutral",
    icon: string,
    title: string,
    sub: string,
  ): void {
    // A clean "done" screen: clear the transcript so only the confirmation remains.
    this.log.replaceChildren();
    const box = el("div", "offboard-final");
    const badge = el("div", `offboard-final-badge${tone === "neutral" ? " neutral" : ""}`);
    badge.innerHTML = icon;
    const t = el("div", "offboard-final-title");
    t.textContent = title;
    const s = el("div", "offboard-final-sub");
    s.textContent = sub;
    box.append(badge, t, s);
    this.log.appendChild(box);
    this.scrollToEnd();
  }

  /** Let the confirming state breathe for a beat, then close and hand off. */
  private finish(cb: () => void): void {
    if (this.closed) return;
    this.closed = true;
    window.setTimeout(() => {
      this.overlay.remove();
      cb();
    }, 1400);
  }

  /** The user is leaving now — escape hatch, or declined offer. */
  private leaveNow(outcome: ResolvedOutcome | null): void {
    if (this.closed) return;
    this.closed = true;
    this.overlay.remove();
    if (outcome) {
      this.reportResolution(false); // an offer was on the table and declined
      this.opts.onCancel?.(outcome);
    } else {
      // Escape hatch before any diagnosis. Support both the documented onCancel(null)
      // and the legacy onJustCancel hook.
      this.opts.onJustCancel?.();
      this.opts.onCancel?.(null);
    }
  }

  private fail(err: unknown): void {
    // On any transport failure we do NOT trap the user — we surface it and keep the
    // escape hatch live so they can still cancel cleanly.
    this.hideTyping();
    this.setStatus("");
    const msg =
      err instanceof OffboardApiError
        ? "Sorry — something went wrong on our end. You can still cancel below."
        : "Sorry — we couldn't reach the server. You can still cancel below.";
    this.appendBot(msg);
    this.setBusy(true);
    this.input.disabled = true;
  }

  private showTyping(): void {
    if (this.typingEl) return;
    const t = el("div", "offboard-typing");
    t.setAttribute("aria-label", "Assistant is typing");
    t.innerHTML = "<span></span><span></span><span></span>";
    this.log.appendChild(t);
    this.typingEl = t;
    this.setStatus("typing…", true);
    this.scrollToEnd();
  }

  private hideTyping(): void {
    this.typingEl?.remove();
    this.typingEl = null;
  }

  private appendBot(text: string): void {
    this.appendMsg(text, "bot");
  }

  private appendUser(text: string): void {
    this.appendMsg(text, "user");
  }

  private appendMsg(text: string, who: "bot" | "user"): void {
    const node = el("div", `offboard-msg ${who}`);
    node.textContent = text;
    // Keep the typing bubble last if it's live.
    if (this.typingEl && who === "user") {
      this.log.insertBefore(node, this.typingEl);
    } else {
      this.log.appendChild(node);
    }
    this.scrollToEnd();
  }

  private setStatus(text: string, live = false): void {
    this.status.innerHTML = "";
    if (!text) return;
    if (live) this.status.append(this.dot());
    this.status.append(document.createTextNode(text));
  }

  private dot(): HTMLSpanElement {
    return el("span", "dot");
  }

  private syncSend(): void {
    this.send.disabled = this.input.value.trim().length === 0 || this.closed;
  }

  private setBusy(busy: boolean): void {
    this.input.disabled = busy;
    if (busy) this.send.disabled = true;
    else this.syncSend();
  }

  private scrollToEnd(): void {
    this.log.scrollTop = this.log.scrollHeight;
  }
}
