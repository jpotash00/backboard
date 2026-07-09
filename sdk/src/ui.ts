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
import type { ShowCancelFlowOptions, UserContext } from "./types.js";

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
  private readonly log: HTMLDivElement;
  private readonly input: HTMLInputElement;
  private readonly send: HTMLButtonElement;
  private sessionId: string | null = null;
  private closed = false;

  constructor(
    private readonly client: SessionClient,
    private readonly opts: ShowCancelFlowOptions,
  ) {
    ensureStyles();

    this.overlay = document.createElement("div");
    this.overlay.className = "offboard-overlay";
    this.overlay.setAttribute("role", "dialog");
    this.overlay.setAttribute("aria-modal", "true");

    const modal = document.createElement("div");
    modal.className = "offboard-modal";

    this.log = document.createElement("div");
    this.log.className = "offboard-log";

    const inputRow = document.createElement("div");
    inputRow.className = "offboard-input-row";
    this.input = document.createElement("input");
    this.input.className = "offboard-input";
    this.input.placeholder = "Type your reply…";
    this.input.autocomplete = "off";
    this.send = document.createElement("button");
    this.send.className = "offboard-send";
    this.send.textContent = "Send";
    inputRow.append(this.input, this.send);

    // Hard constraint #3: the escape hatch is always here.
    const escape = document.createElement("button");
    escape.className = "offboard-escape";
    escape.textContent = opts.justCancelLabel ?? "Just cancel";
    escape.addEventListener("click", () => this.justCancel());

    modal.append(this.log, inputRow, escape);
    this.overlay.append(modal);

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
        this.close();
        this.opts.onResolved({
          ...res.outcome,
          intervention: res.intervention ?? null,
        });
        return;
      }
    } catch (err) {
      this.fail(err);
      return;
    }
    this.setBusy(false);
    this.input.focus();
  }

  private justCancel(): void {
    if (this.closed) return;
    this.close();
    this.opts.onJustCancel?.();
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

  private appendMsg(text: string, who: "bot" | "user"): void {
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
