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
 * Design intent: it should look like the app it's embedded in, not like a third-party
 * chatbot bolted on. The surface uses a modern AI-chat layout (full-width assistant turns
 * with an avatar, only the user gets a bubble, an auto-growing composer with the send
 * control inside the field) rendered in a neutral shadcn/ui token system. Because it's a
 * drop-in widget it can't require React/Tailwind on the host, so it adopts the host's
 * *design language* instead: by default a self-contained neutral palette, and via
 * `theme.adoptHostTokens` it inherits the host page's shadcn CSS variables directly, so
 * the widget takes on the customer's colours and radius (light and dark). `theme.accent`
 * and per-token overrides let any host match explicitly. Someone is leaving — nothing
 * should feel like a trap; the exit stays dignified and one tap away.
 */

import { OffboardApiError, SessionClient } from "./client.js";
import type {
  OffboardTheme,
  ResolvedOutcome,
  ShowCancelFlowOptions,
  UserContext,
} from "./types.js";

const STYLE_ID = "offboard-styles";

/**
 * All styles are scoped under `.offboard-overlay` and driven by CSS variables so
 * light/dark are one source of truth. The palette is shadcn/ui's neutral (zinc) scale in
 * HSL, held in two layers:
 *   - `--ob-*-default` — the built-in values (flipped for dark by the media query below).
 *   - `--ob-*`         — what the widget actually paints with. By default these ARE the
 *                        built-ins; with `.ob-adopt` (opt-in, set by `theme.adoptHostTokens`)
 *                        they inherit the host page's shadcn variables instead, so the widget
 *                        looks like the app it's embedded in. Per-token `theme` overrides are
 *                        applied inline on the overlay and win over both.
 * `--ob-accent` follows `--ob-primary` (the neutral monochrome CTA) unless a host points it
 * at a brand colour. The two-layer split matters: the dark media query only flips the
 * *-default fallbacks, so host adoption (the host's own light/dark) is never clobbered by the
 * viewer's OS theme.
 */
const CSS = `
.offboard-overlay,.offboard-overlay *{ box-sizing:border-box }
.offboard-overlay{
  --ob-bg-default:0 0% 100%; --ob-fg-default:240 10% 3.9%;
  --ob-muted-default:240 4.8% 95.9%; --ob-muted-fg-default:240 3.8% 46.1%;
  --ob-border-default:240 5.9% 90%; --ob-ring-default:240 5% 34%;
  --ob-primary-default:240 5.9% 10%; --ob-primary-fg-default:0 0% 98%;

  --ob-bg:var(--ob-bg-default); --ob-fg:var(--ob-fg-default);
  --ob-muted:var(--ob-muted-default); --ob-muted-fg:var(--ob-muted-fg-default);
  --ob-border:var(--ob-border-default); --ob-ring:var(--ob-ring-default);
  --ob-primary:var(--ob-primary-default); --ob-primary-fg:var(--ob-primary-fg-default);
  --ob-accent:var(--ob-primary); --ob-accent-fg:var(--ob-primary-fg);
  --ob-good:142 71% 35%; --ob-good-soft:142 71% 35%;
  --ob-radius:.65rem;
  --ob-shadow:0 10px 15px -3px rgba(0,0,0,.08), 0 4px 6px -4px rgba(0,0,0,.08),
    0 40px 80px -20px rgba(0,0,0,.22);
  --ob-ease:cubic-bezier(.32,.72,0,1);
  position:fixed; inset:0; z-index:2147483647;
  display:flex; align-items:stretch; justify-content:center;
  padding:0; background:hsl(240 10% 3.9% / .32);
  -webkit-backdrop-filter:blur(8px) saturate(120%); backdrop-filter:blur(8px) saturate(120%);
  opacity:0; animation:ob-fade .28s var(--ob-ease) forwards;
  font-family:ui-sans-serif,-apple-system,BlinkMacSystemFont,"Segoe UI",Inter,Roboto,
    "Helvetica Neue",Arial,sans-serif;
  -webkit-font-smoothing:antialiased; text-rendering:optimizeLegibility;
}
@media (prefers-color-scheme: dark){
  .offboard-overlay{
    --ob-bg-default:240 10% 6%; --ob-fg-default:0 0% 98%;
    --ob-muted-default:240 3.7% 15.9%; --ob-muted-fg-default:240 5% 64.9%;
    --ob-border-default:240 3.7% 18%; --ob-ring-default:240 4.9% 64%;
    --ob-primary-default:0 0% 98%; --ob-primary-fg-default:240 5.9% 10%;
    --ob-good:142 69% 58%; --ob-good-soft:142 69% 58%;
    --ob-shadow:0 10px 15px -3px rgba(0,0,0,.5), 0 40px 80px -20px rgba(0,0,0,.6);
    background:hsl(0 0% 0% / .55);
  }
}
/* Opt-in host adoption: inherit the app's shadcn tokens, built-ins as the fallback. Explicit
   theme overrides are set inline on the overlay, so they still win over these. */
.offboard-overlay.ob-adopt{
  --ob-bg:var(--background, var(--ob-bg-default));
  --ob-fg:var(--foreground, var(--ob-fg-default));
  --ob-muted:var(--muted, var(--ob-muted-default));
  --ob-muted-fg:var(--muted-foreground, var(--ob-muted-fg-default));
  --ob-border:var(--border, var(--ob-border-default));
  --ob-ring:var(--ring, var(--ob-ring-default));
  --ob-primary:var(--primary, var(--ob-primary-default));
  --ob-primary-fg:var(--primary-foreground, var(--ob-primary-fg-default));
  --ob-radius:var(--radius, .65rem);
}
/* Phone: a full-screen conversation (100dvh dodges the iOS URL-bar jump), composer
   pinned above the keyboard. Tablet/desktop: a centered, roomy card. */
@media (min-width:640px){ .offboard-overlay{ align-items:center; padding:24px } }

.offboard-modal{
  position:relative; display:flex; flex-direction:column;
  width:100%; height:100dvh; max-width:none;
  background:hsl(var(--ob-bg)); color:hsl(var(--ob-fg));
  border-radius:0; box-shadow:var(--ob-shadow); overflow:hidden;
  transform:translateY(10px); opacity:0;
  animation:ob-rise .42s var(--ob-ease) .02s forwards;
}
@media (min-width:640px){
  .offboard-modal{
    height:auto; max-width:480px; max-height:min(86vh,760px);
    border:1px solid hsl(var(--ob-border));
    border-radius:calc(var(--ob-radius) + 10px);
    transform:translateY(8px) scale(.985);
  }
}

/* Header — a quiet identity so the conversation feels attended, not automated. */
.offboard-head{ display:flex; align-items:center; gap:11px; flex:0 0 auto;
  padding:14px 18px; border-bottom:1px solid hsl(var(--ob-border));
  padding-top:calc(14px + env(safe-area-inset-top,0px)) }
@media (min-width:640px){ .offboard-head{ padding-top:14px } }
.offboard-avatar{ width:30px; height:30px; border-radius:9px; flex:0 0 auto;
  display:grid; place-items:center; background:hsl(var(--ob-primary));
  color:hsl(var(--ob-primary-fg)) }
.offboard-avatar svg{ width:17px; height:17px }
.offboard-head-text{ display:flex; flex-direction:column; min-width:0; gap:2px }
.offboard-title{ font-size:14.5px; font-weight:600; letter-spacing:-.01em; line-height:1.1 }
.offboard-status{ font-size:12px; color:hsl(var(--ob-muted-fg)); line-height:1.2; height:14px;
  display:flex; align-items:center; gap:6px }
.offboard-status .dot{ width:6px; height:6px; border-radius:50%; background:hsl(var(--ob-good));
  box-shadow:0 0 0 0 hsl(var(--ob-good) / .35); animation:ob-pulse 2.4s ease-in-out infinite }

/* Message log */
/* min-height:0 is load-bearing: without it a flex child won't shrink below its content height,
   so the log stops scrolling and a tall offer card gets clipped by the modal's overflow:hidden. */
.offboard-log{ flex:1 1 auto; min-width:0; min-height:0; overflow-y:auto; -webkit-overflow-scrolling:touch;
  display:flex; flex-direction:column; gap:18px; padding:22px 18px 8px;
  scrollbar-width:thin; scrollbar-color:hsl(var(--ob-border)) transparent }
.offboard-log::-webkit-scrollbar{ width:8px }
.offboard-log::-webkit-scrollbar-thumb{ background:hsl(var(--ob-border)); border-radius:8px }

/* A turn = optional avatar + content. Assistant is full-width, borderless (Claude/
   ChatGPT). The user gets a subtle muted bubble on the right. */
.offboard-row{ display:flex; gap:11px; align-items:flex-start; max-width:100%;
  animation:ob-pop .38s var(--ob-ease) both }
.offboard-row.user{ justify-content:flex-end }
.offboard-row-avatar{ width:26px; height:26px; border-radius:8px; flex:0 0 auto; margin-top:1px;
  display:grid; place-items:center; background:hsl(var(--ob-muted)); color:hsl(var(--ob-fg)) }
.offboard-row-avatar svg{ width:15px; height:15px }
.offboard-bubble{ font-size:15px; line-height:1.6; letter-spacing:-.006em; word-wrap:break-word;
  white-space:pre-wrap }
.offboard-bubble.bot{ color:hsl(var(--ob-fg)); padding-top:2px; max-width:calc(100% - 37px) }
.offboard-bubble.user{ max-width:84%; padding:9px 14px; border-radius:18px;
  background:hsl(var(--ob-muted)); color:hsl(var(--ob-fg)) }

/* Typing indicator — three dots in an assistant row while the engine thinks. */
.offboard-typing{ display:flex; gap:5px; align-items:center; padding:6px 0 }
.offboard-typing span{ width:7px; height:7px; border-radius:50%; background:hsl(var(--ob-muted-fg));
  opacity:.5; animation:ob-blink 1.3s ease-in-out infinite }
.offboard-typing span:nth-child(2){ animation-delay:.18s }
.offboard-typing span:nth-child(3){ animation-delay:.36s }

/* Composer — an auto-growing textarea with the send control inside the field, the way
   Claude and ChatGPT do it. The whole box lights a ring on focus. */
.offboard-foot{ flex:0 0 auto; padding:10px 16px calc(12px + env(safe-area-inset-bottom,0px));
  background:hsl(var(--ob-bg)) }
.offboard-composer{ display:flex; align-items:flex-end; gap:8px;
  border:1px solid hsl(var(--ob-border)); background:hsl(var(--ob-bg));
  border-radius:calc(var(--ob-radius) + 10px); padding:7px 7px 7px 15px;
  transition:border-color .16s ease, box-shadow .16s ease }
.offboard-composer:focus-within{ border-color:hsl(var(--ob-ring));
  box-shadow:0 0 0 3px hsl(var(--ob-ring) / .16) }
.offboard-input{ flex:1 1 auto; min-width:0; border:0; background:transparent; resize:none;
  outline:none; color:hsl(var(--ob-fg)); font:inherit; font-size:15px; line-height:1.5;
  max-height:140px; padding:7px 0 }
.offboard-input::placeholder{ color:hsl(var(--ob-muted-fg)) }
.offboard-send{ flex:0 0 auto; width:34px; height:34px; border:0; cursor:pointer;
  border-radius:calc(var(--ob-radius) + 1px); background:hsl(var(--ob-accent));
  color:hsl(var(--ob-accent-fg)); display:grid; place-items:center;
  transition:transform .16s var(--ob-ease), opacity .16s ease, filter .16s ease }
.offboard-send svg{ width:18px; height:18px }
.offboard-send:not(:disabled):hover{ filter:brightness(1.08) }
.offboard-send:not(:disabled):active{ transform:scale(.92) }
.offboard-send:disabled{ opacity:.3; cursor:default }

/* The escape hatch — hard constraint #3. Understated so it doesn't compete with the
   conversation, but underlined so it unmistakably reads as a tap-able action and never
   as decorative caption text: calm is not the same as buried. */
.offboard-escape{ display:block; margin:8px auto 0; padding:6px 10px; border:0;
  background:transparent; color:hsl(var(--ob-muted-fg)); font:inherit; font-size:13px;
  cursor:pointer; border-radius:8px; text-decoration:underline; text-underline-offset:3px;
  text-decoration-color:hsl(var(--ob-muted-fg) / .5);
  transition:color .16s ease, background .16s ease, text-decoration-color .16s ease }
.offboard-escape:hover,.offboard-escape:focus-visible{ color:hsl(var(--ob-fg));
  background:hsl(var(--ob-muted)); text-decoration-color:hsl(var(--ob-fg)) }

/* In-chat offer card — the personalized "one reason to stay", presented calmly. */
.offboard-offer{ align-self:stretch; max-width:100%; margin:2px 0; border-radius:calc(var(--ob-radius) + 4px);
  border:1px solid hsl(var(--ob-border)); background:hsl(var(--ob-bg));
  box-shadow:0 1px 2px rgba(0,0,0,.04); overflow:hidden;
  animation:ob-pop .44s var(--ob-ease) both }
.offboard-offer-top{ padding:16px 18px 14px }
.offboard-offer-eyebrow{ display:flex; align-items:center; gap:7px; font-size:11.5px; font-weight:600;
  letter-spacing:.04em; text-transform:uppercase; color:hsl(var(--ob-muted-fg)) }
.offboard-offer-eyebrow svg{ width:14px; height:14px }
.offboard-offer-headline{ margin-top:9px; font-size:18px; font-weight:600; letter-spacing:-.02em;
  line-height:1.3; color:hsl(var(--ob-fg)) }
.offboard-offer-sub{ margin-top:6px; font-size:13.5px; line-height:1.55; color:hsl(var(--ob-muted-fg)) }
.offboard-offer-actions{ display:flex; flex-direction:column; gap:8px; padding:2px 14px 14px }
.offboard-btn{ width:100%; padding:11px 16px; border-radius:var(--ob-radius); font:inherit;
  font-size:14.5px; font-weight:600; letter-spacing:-.01em; cursor:pointer;
  border:1px solid transparent;
  transition:transform .16s var(--ob-ease), background .16s ease, filter .16s ease, opacity .16s ease }
.offboard-btn:active{ transform:scale(.985) }
.offboard-btn-primary{ background:hsl(var(--ob-accent)); color:hsl(var(--ob-accent-fg)) }
.offboard-btn-primary:hover{ filter:brightness(1.08) }
.offboard-btn-ghost{ background:transparent; color:hsl(var(--ob-fg)); border-color:hsl(var(--ob-border)) }
.offboard-btn-ghost:hover{ background:hsl(var(--ob-muted)) }

/* Terminal confirmation — a graceful close, whether they stayed or left. */
.offboard-final{ align-self:stretch; max-width:100%; text-align:center; margin:auto 0;
  padding:44px 24px; animation:ob-pop .42s var(--ob-ease) both }
.offboard-final-badge{ width:52px; height:52px; margin:0 auto 16px; border-radius:50%;
  display:grid; place-items:center; background:hsl(var(--ob-good) / .12); color:hsl(var(--ob-good)) }
.offboard-final-badge svg{ width:26px; height:26px }
.offboard-final-badge.neutral{ background:hsl(var(--ob-muted)); color:hsl(var(--ob-muted-fg)) }
.offboard-final-title{ font-size:17px; font-weight:600; letter-spacing:-.015em; color:hsl(var(--ob-fg)) }
.offboard-final-sub{ margin-top:7px; font-size:14px; line-height:1.55; color:hsl(var(--ob-muted-fg));
  max-width:34ch; margin-left:auto; margin-right:auto }

@keyframes ob-fade{ to{ opacity:1 } }
@keyframes ob-rise{ to{ transform:none; opacity:1 } }
@keyframes ob-pop{ from{ opacity:0; transform:translateY(6px) } to{ opacity:1; transform:none } }
@keyframes ob-blink{ 0%,60%,100%{ opacity:.5; transform:translateY(0) }
  30%{ opacity:1; transform:translateY(-2px) } }
@keyframes ob-pulse{ 0%,100%{ box-shadow:0 0 0 0 hsl(var(--ob-good) / .35) }
  50%{ box-shadow:0 0 0 5px transparent } }

@media (prefers-reduced-motion: reduce){
  .offboard-overlay,.offboard-modal,.offboard-row,.offboard-offer,
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
  private readonly input: HTMLTextAreaElement;
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
    // Make it look like the host app: opt-in host-token adoption + explicit overrides.
    this.applyTheme(opts.theme);

    const modal = el("div", "offboard-modal");

    // Header identity — a quiet signal that a person (not a wall) is listening.
    const head = el("div", "offboard-head");
    const avatar = el("div", "offboard-avatar");
    avatar.innerHTML = SPARK_ICON;
    const headText = el("div", "offboard-head-text");
    const title = el("div", "offboard-title");
    title.textContent = "Before you go";
    this.status = el("div", "offboard-status");
    headText.append(title, this.status);
    head.append(avatar, headText);

    this.log = el("div", "offboard-log");

    // Composer — an auto-growing textarea with the send control inside the field.
    this.foot = el("div", "offboard-foot");
    const composer = el("div", "offboard-composer");
    this.input = el("textarea", "offboard-input");
    this.input.rows = 1;
    this.input.placeholder = "Type your reply…";
    this.input.setAttribute("autocomplete", "off");
    this.input.setAttribute("aria-label", "Your reply");
    this.send = el("button", "offboard-send");
    this.send.type = "button";
    this.send.innerHTML = SEND_ICON;
    this.send.setAttribute("aria-label", "Send");
    this.send.disabled = true;
    composer.append(this.input, this.send);

    // Hard constraint #3: the escape hatch is always here, one tap away.
    const escape = el("button", "offboard-escape");
    escape.type = "button";
    escape.textContent = this.opts.justCancelLabel ?? "Just cancel my subscription";
    escape.addEventListener("click", () => this.leaveNow(null));

    this.foot.append(composer, escape);
    modal.append(head, this.log, this.foot);
    this.overlay.append(modal);

    this.send.addEventListener("click", () => void this.submit());
    this.input.addEventListener("input", () => {
      this.syncSend();
      this.autoGrow();
    });
    // Enter sends; Shift+Enter (and Enter on touch keyboards) inserts a newline.
    this.input.addEventListener("keydown", (e) => {
      if (e.key === "Enter" && !e.shiftKey && !this.isCoarsePointer()) {
        e.preventDefault();
        void this.submit();
      }
    });
  }

  /**
   * Make the widget match the host app. `adoptHostTokens` flips on the `.ob-adopt` class so
   * the scoped CSS inherits the page's shadcn variables; explicit tokens are set inline on the
   * overlay (highest precedence) so they win over both adoption and the built-in defaults.
   * Values are raw HSL triples (`"222 47% 11%"`) — validated loosely so a stray `#hex`/`hsl()`
   * can't inject arbitrary CSS through `setProperty`.
   */
  private applyTheme(theme?: OffboardTheme): void {
    if (!theme) return;
    if (theme.adoptHostTokens) this.overlay.classList.add("ob-adopt");
    const set = (value: string | undefined, prop: string, triple = true): void => {
      if (!value) return;
      if (triple && !/^[0-9.\s%]+$/.test(value)) return; // reject anything but an HSL triple
      this.overlay.style.setProperty(prop, value);
    };
    set(theme.background, "--ob-bg");
    set(theme.foreground, "--ob-fg");
    set(theme.muted, "--ob-muted");
    set(theme.mutedForeground, "--ob-muted-fg");
    set(theme.border, "--ob-border");
    set(theme.ring, "--ob-ring");
    set(theme.primary, "--ob-primary");
    set(theme.primaryForeground, "--ob-primary-fg");
    set(theme.accent, "--ob-accent");
    set(theme.accentForeground, "--ob-accent-fg");
    set(theme.radius, "--ob-radius", false); // a CSS length, not a colour triple
  }

  /** Grow the textarea to fit its content, up to the CSS max-height then scroll. */
  private autoGrow(): void {
    this.input.style.height = "auto";
    this.input.style.height = `${this.input.scrollHeight}px`;
  }

  /** On phones the on-screen keyboard's Enter should add a line, not send — the send
   * button is the deliberate action there, matching Claude/ChatGPT mobile. */
  private isCoarsePointer(): boolean {
    return typeof window !== "undefined" &&
      typeof window.matchMedia === "function" &&
      window.matchMedia("(pointer: coarse)").matches;
  }

  async open(): Promise<void> {
    document.body.appendChild(this.overlay);
    this.input.focus();
    this.setStatus("connecting…");
    this.showTyping();
    const user: UserContext = { user_id: this.opts.userId, ...this.opts.context };
    try {
      const res = await this.client.open(user, this.opts.identityToken);
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
    this.input.style.height = "auto";
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
    // Honest, non-scarcity framing by default: the offer earns trust from the diagnosis it's
    // tied to (the sub line below), not from "exclusive / just for you" theatre.
    const eyebrowLabel = el("span");
    eyebrowLabel.textContent = this.opts.offerEyebrow ?? "Here's what I can do";
    eyebrow.innerHTML = SPARK_ICON;
    eyebrow.appendChild(eyebrowLabel);
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
    // The three dots live inside an assistant row, so they sit exactly where the
    // reply will render — the avatar stays put and the text simply resolves in.
    const row = this.assistantRow();
    const dots = el("div", "offboard-typing");
    dots.setAttribute("aria-label", "Assistant is typing");
    dots.innerHTML = "<span></span><span></span><span></span>";
    row.appendChild(dots);
    this.log.appendChild(row);
    this.typingEl = row;
    this.setStatus("typing…", true);
    this.scrollToEnd();
  }

  private hideTyping(): void {
    this.typingEl?.remove();
    this.typingEl = null;
  }

  private appendBot(text: string): void {
    const row = this.assistantRow();
    const bubble = el("div", "offboard-bubble bot");
    bubble.textContent = text;
    row.appendChild(bubble);
    this.placeRow(row, "bot");
  }

  private appendUser(text: string): void {
    const row = el("div", "offboard-row user");
    const bubble = el("div", "offboard-bubble user");
    bubble.textContent = text;
    row.appendChild(bubble);
    this.placeRow(row, "user");
  }

  /** An assistant turn: the avatar (its identity) followed by content. */
  private assistantRow(): HTMLDivElement {
    const row = el("div", "offboard-row bot");
    const avatar = el("div", "offboard-row-avatar");
    avatar.innerHTML = SPARK_ICON;
    row.appendChild(avatar);
    return row;
  }

  private placeRow(row: HTMLDivElement, who: "bot" | "user"): void {
    // Keep the live typing row last so the user's message slots in above it.
    if (this.typingEl && who === "user") {
      this.log.insertBefore(row, this.typingEl);
    } else {
      this.log.appendChild(row);
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
