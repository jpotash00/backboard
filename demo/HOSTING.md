# Hosting the demo

There are two demo surfaces in this folder:

- **[index.html](index.html)** — the minimal mock billing card (Acme ↔ Zen toggle).
- **[app.html](app.html)** — a full "Acme Analytics" product shell (sidebar, dashboard, Settings →
  Billing) with the cancel flow wired into a realistic billing page. This is the one to show.

`app.html` is **self-contained and dual-mode**. It decides how to run from the URL it's served at:

| Served from | SDK source | Engine (`API_BASE`) | Key |
| --- | --- | --- | --- |
| `localhost` / `file://` | `../sdk/dist/index.js` (local build) | `http://localhost:8000` | `pk_demo_acme` |
| anywhere else | `https://esm.sh/offboard` (published SDK) | `https://offboard.fly.dev` | `PROD_KEY` |

## Live now

The engine serves it directly at **https://offboard.fly.dev/demo** (route: `GET /demo` in
[api/app.py](../api/app.py)). That works because:

1. The image ships `demo/` and `configs/` (see the [Dockerfile](../Dockerfile)).
2. `OFFBOARD_SEED_DEMO=1` (in [fly.toml](../fly.toml)) makes the app copy the two **secret-less**
   demo tenants (`configs/acme.json`, `configs/zen.json`) onto the `/data/configs` volume on boot.
   Secret-less = *trust-body* mode, so the browser can supply the persona's economics with **no
   signed identity token** — which is exactly what a live-in-browser demo needs.

To re-seed a fresh volume, redeploy (seeding is idempotent and never overwrites an onboarded
tenant). To stop serving the demo, remove `OFFBOARD_SEED_DEMO` and the `GET /demo` route.

## Hosting the file elsewhere (Netlify / Vercel / Pages)

Drop `app.html` on any static host. It'll auto-select CDN + prod. You only need to set `PROD_KEY`
to a **secret-less** demo tenant that exists on the engine — the seeded `pk_demo_acme` already
qualifies. If that tenant sets `allowed_origins`, add your static host's origin; the seeded demo
configs leave it empty (any origin allowed), so nothing to do.

> ⚠️ A publishable key is public by design, but it can create sessions (each is a model call).
> The demo tenants are rate-limited per key + per IP. If abuse ever matters, rotate the demo key
> or lower the limits.
