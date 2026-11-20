#!/usr/bin/env node
// `npx offboard init` -- register your product with Offboard from the terminal.
// Asks for the same fields as the /onboard web page, POSTs them to /onboard, and prints the
// keys to paste into your app. Node 18+ (built-in fetch + readline), no dependencies.

import { createInterface } from "node:readline/promises";
import { stdin, stdout, argv, env, exit } from "node:process";

const API = (env.OFFBOARD_API_URL || "https://offboard.fly.dev").replace(/\/$/, "");
// Mirrors onboarding/proposer.py KNOWN_TYPES: the offer kinds the policy engine can route.
const OFFER_TYPES = ["discount", "downgrade", "pause", "onboarding", "roadmap", "support", "gift", "extend_trial"];

const slugify = (s) => s.toLowerCase().trim().replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "").slice(0, 200);
const csv = (s) => s.split(",").map((x) => x.trim()).filter(Boolean);

if (argv[2] !== "init") {
  console.log("usage: npx offboard init\n\nenv: OFFBOARD_SIGNUP_CODE, OFFBOARD_API_URL (default https://offboard.fly.dev)");
  exit(argv[2] ? 1 : 0);
}

const rl = createInterface({ input: stdin, output: stdout });
// Pull lines from the iterator (it buffers) rather than rl.question(), which drops lines that
// arrive before it's called -- so piped/scripted answers work, not just a typing human.
const lines = rl[Symbol.asyncIterator]();
const ask = async (q, { required = true, def = "" } = {}) => {
  for (;;) {
    stdout.write(def ? `? ${q} (${def}) ` : `? ${q} `);
    const { value, done } = await lines.next();
    if (done) { console.error("\n✖ input ended before setup finished"); exit(1); }
    const a = value.trim() || def;
    if (a || !required) return a;
    console.log("  required");
  }
};

console.log("Offboard setup: registers your product and prints your keys.\n");
let code = env.OFFBOARD_SIGNUP_CODE || (await ask("Signup code"));

const product = {
  product_name: await ask("Product name"),
  product_context: await ask("What it does (one or two sentences: what, for whom)"),
  activation_definition: await ask('What "activated" means (the moment a user has really adopted you)'),
  pricing_summary: await ask("Pricing (plans and prices)"),
};
const allowed_origins = csv(await ask("Your site's URL(s), comma-separated (optional)", { required: false }));

console.log(`\nSave offers: what you authorize. Offboard never invents one.\nTypes: ${OFFER_TYPES.join(", ")}`);
const offers = [];
do {
  let type;
  while (!OFFER_TYPES.includes((type = await ask("Offer type", { def: "discount" })))) {
    console.log(`  pick one of: ${OFFER_TYPES.join(", ")}`);
  }
  offers.push({ type, description: await ask("Description (e.g. 40% off for 3 months)") });
} while (/^y/i.test(await ask("Add another offer? (y/N)", { required: false })));

console.log("\nOffboard runs interviews on YOUR Claude account (get a key at console.anthropic.com).");
let anthropic_api_key;
while (!(anthropic_api_key = await ask("Anthropic API key (stored encrypted, never shown again)")).startsWith("sk-ant-")) {
  console.log('  that doesn\'t look like an Anthropic key (starts with "sk-ant-")');
}

let customer_id = slugify(product.product_name);
for (;;) {
  if (!customer_id) customer_id = slugify(await ask("Account handle (lowercase, no spaces)"));
  const spec = { customer_id, product, offers, allowed_origins, anthropic_api_key };
  let res;
  try {
    res = await fetch(`${API}/onboard`, {
      method: "POST",
      headers: { "Content-Type": "application/json", Authorization: `Bearer ${code}` },
      body: JSON.stringify(spec),
    });
  } catch (e) {
    console.error(`\n✖ Could not reach ${API}: ${e.message}`);
    exit(1);
  }
  const body = await res.json().catch(() => ({}));
  if (res.status === 201) {
    console.log(`\n✔ Registered ${body.customer_id} with ${body.offers} offer(s)\n`);
    console.log(`  public_key      ${body.public_key}\n                  → your frontend: Offboard.init({ publicKey: "${body.public_key}" })`);
    console.log(`  signing_secret  ${body.signing_secret}\n                  → your backend env only. Shown ONCE; save it now.`);
    console.log("\n  Next: npm install offboard, then see https://www.npmjs.com/package/offboard");
    break;
  }
  if (res.status === 401) { console.log("  ✖ Invalid signup code"); code = await ask("Signup code"); continue; }
  if (res.status === 409) { console.log(`  ✖ "${customer_id}" is taken`); customer_id = ""; continue; }
  if (res.status === 404) { console.error("\n✖ Self-serve signup is not enabled on this server."); exit(1); }
  console.error(`\n✖ ${res.status}: ${JSON.stringify(body.detail ?? body)}`);
  exit(1);
}
rl.close();
