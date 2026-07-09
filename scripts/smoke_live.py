"""Live end-to-end smoke test: does the whole thing actually work?

Drives a REAL cancellation through a RUNNING engine (real model calls) and checks the
result — no browser, deterministic PASS/FAIL. This is the "how do I know it works" answer
for CI, a pre-deploy gate, or a post-deploy check against prod.

    # 1. start the engine (real key, demo customers)
    set -a; . ./.env; set +a
    OFFBOARD_CONFIG_DIR=configs python -m api.app        # :8000

    # 2. in another shell, run this against it
    python scripts/smoke_live.py                          # localhost:8000, pk_demo_acme
    python scripts/smoke_live.py https://api.your.dev pk_live_acme_xxx   # against prod

Exit 0 = the loop works AND the cover story was penetrated. Exit 1 = something's off.

The persona: a user who signed up, never set the product up, and gives "too expensive" as
the polite exit line. A working engine diagnoses an ACTIVATION failure (not price) and offers
help getting started (not a discount). That single result is the product thesis.
"""

import json
import sys
import urllib.error
import urllib.request

BASE = sys.argv[1].rstrip("/") if len(sys.argv) > 1 else "http://localhost:8000"
KEY = sys.argv[2] if len(sys.argv) > 2 else "pk_demo_acme"

# Persona-consistent replies. The interviewer asks freely; these answers fit "I meant to use
# it, never got around to it, and 'too expensive' is really just my exit excuse."
OPENER = "Honestly it's just too expensive for me right now."
FOLLOWUPS = [
    "To be real, I signed up months ago and never actually set it up. Life got busy.",
    "No, I never connected any data or built a dashboard. Never got past the login screen.",
    "If I'm honest, price is the easy thing to say. I just never got it off the ground.",
    "Maybe. If getting started were easier I'd probably give it another shot.",
]


def post(path, body, headers):
    req = urllib.request.Request(
        BASE + path, data=json.dumps(body).encode(), method="POST",
        headers={"Content-Type": "application/json", **headers},
    )
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read())


def main():
    auth = {"Authorization": f"Bearer {KEY}"}
    print(f"→ engine: {BASE}   key: {KEY}\n")

    # A user who never activated. Economic fields are trusted here because the demo customers
    # have no signing_secret (dev mode); a prod customer would pass identity_token instead.
    sess = post("/sessions", {
        "user_id": "smoke_never_activated",
        "plan": "Growth", "mrr": 199.0,
        "tenure_days": 90, "logins_last_30d": 0, "activated": False,
        "usage_summary": "Signed up 3 months ago, one login, no data source connected, no dashboards.",
    }, auth)
    sid = sess["session_id"]
    print(f"assistant: {sess['message']}\n")

    replies = [OPENER, *FOLLOWUPS]
    outcome = intervention = None
    for i, reply in enumerate(replies):
        print(f"user: {reply}")
        turn = post(f"/sessions/{sid}/turn", {"user_message": reply}, auth)
        if turn.get("message"):
            print(f"assistant: {turn['message']}\n")
        if turn.get("done"):
            outcome = turn.get("outcome")
            intervention = turn.get("intervention")
            break

    if not outcome:
        print("\n✗ FAIL: interview never reached a diagnosis within the turn budget.")
        return 1

    reason = outcome.get("reason", "")
    cover = outcome.get("cover_story", "")
    itype = (intervention or {}).get("type", "")
    print("─" * 60)
    print(f"cover story (what they said) : {cover}")
    print(f"diagnosis  (what's real)     : {reason}   [conf {outcome.get('confidence')}, mode {outcome.get('mode')}]")
    print(f"evidence                     : {outcome.get('evidence')}")
    print(f"authorized offer             : {itype} — {(intervention or {}).get('description')}")
    print("─" * 60)

    # Assertions: the cover story was penetrated, and we did NOT reflexively hand out a discount
    # to someone whose problem is that they never started.
    penetrated = "activat" in reason.lower() or "never" in reason.lower() or "onboard" in reason.lower()
    not_a_discount = itype != "discount"
    ok = penetrated and not_a_discount
    print("PASS ✓" if ok else "FAIL ✗", "— cover story penetrated:", penetrated,
          "| avoided reflexive discount:", not_a_discount)
    return 0 if ok else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except urllib.error.HTTPError as e:
        print(f"✗ HTTP {e.code}: {e.read().decode()[:400]}")
        sys.exit(1)
    except urllib.error.URLError as e:
        print(f"✗ can't reach {BASE} — is the engine running? ({e.reason})")
        sys.exit(1)
