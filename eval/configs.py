"""The test customer's config. In production this is registered once per customer
(and eventually auto-proposed by scraping their pricing page + docs -- the brief's
"hidden product"). For the eval it's a fixture.

The intervention menu here is deliberately fuller than the §4 example so that every
taxonomy reason has a concrete authorized action to resolve to -- otherwise
intervention-correctness would be measuring the menu's gaps, not the policy's choices.
"""

from engine import Intervention, ProductConfig

ACME = ProductConfig(
    product_name="Acme Analytics",
    product_context="Self-serve product analytics for small SaaS teams. Connect a data "
                    "source, build dashboards, track activation and retention funnels.",
    activation_definition="connected a data source and viewed at least one dashboard",
    pricing_summary="$49/mo Starter (1 source, 10k events/mo), $199/mo Growth "
                    "(unlimited sources, 1M events/mo)",
    known_churn_reasons=[
        "signed up but never connected a data source",
        "outgrew us and moved to Amplitude",
        "hit Starter event limits and balked at the Growth price",
    ],
    competitors=["Amplitude", "Mixpanel", "PostHog"],
    interventions=[
        Intervention(
            id="discount_50_3mo", type="discount",
            description="50% off for 3 months",
            eligible_when="reason == price_value_mismatch AND tenure > 90",
        ),
        Intervention(
            id="downgrade_starter", type="downgrade",
            description="Move down to the Starter plan",
        ),
        Intervention(
            id="pause_3mo", type="pause",
            description="Pause the subscription for up to 3 months",
        ),
        Intervention(
            id="setup_call_15m", type="onboarding",
            description="Free 15-minute setup call to connect your first data source",
        ),
        Intervention(
            id="roadmap_notify", type="roadmap",
            description="Get notified the moment the feature you need ships",
        ),
        Intervention(
            id="priority_support", type="support",
            description="Priority support with an engineer to fix outstanding issues",
        ),
    ],
)
