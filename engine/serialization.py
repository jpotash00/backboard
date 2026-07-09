"""(De)serialize a ProductConfig to/from plain JSON-able dicts.

This is the seam that turns a config from in-code Python into a stored, versioned
record: a DB row, a file, or the output of the "scrape pricing page -> propose config"
onboarding step. `config_from_dict` validates on the way in, so a malformed stored
config fails loudly at load, never silently mis-routes a live cancel flow.

Partial configs are welcome: omit `reasons` or `policy` and the customer inherits the
default SaaS taxonomy / rulebook. Only the four product fields are required.
"""

import json
from typing import Any

from .taxonomy import (
    DEFAULT_REASONS,
    Experiment,
    Intervention,
    Policy,
    ProductConfig,
    ReasonDef,
    Scoring,
)


def config_to_dict(c: ProductConfig) -> dict[str, Any]:
    """A JSON-safe dict. Policy's sets become sorted lists (JSON has no set)."""
    return {
        "config_version": c.config_version,
        "product_name": c.product_name,
        "product_context": c.product_context,
        "activation_definition": c.activation_definition,
        "pricing_summary": c.pricing_summary,
        "known_churn_reasons": list(c.known_churn_reasons),
        "competitors": list(c.competitors),
        "reasons": [{"id": r.id, "description": r.description} for r in c.reasons],
        "interventions": [
            {"id": i.id, "type": i.type, "description": i.description,
             "eligible_when": i.eligible_when}
            for i in c.interventions
        ],
        "policy": {
            "confidence_floor": c.policy.confidence_floor,
            "preferred": {k: list(v) for k, v in c.policy.preferred.items()},
            "discount_reasons": sorted(c.policy.discount_reasons),
            "let_go_reasons": sorted(c.policy.let_go_reasons),
            "rank_by": c.policy.rank_by,
            "corroboration": dict(c.policy.corroboration),
            "contradiction_penalty": c.policy.contradiction_penalty,
            "act_confidence": c.policy.act_confidence,
            "scoring": {
                "value_horizon_months": c.policy.scoring.value_horizon_months,
                "type_cost": dict(c.policy.scoring.type_cost),
                "save_prior": dict(c.policy.scoring.save_prior),
                "type_effectiveness": dict(c.policy.scoring.type_effectiveness),
                "default_type_cost": c.policy.scoring.default_type_cost,
                "default_save_prior": c.policy.scoring.default_save_prior,
                "default_effectiveness": c.policy.scoring.default_effectiveness,
                "min_confidence_by_type": dict(c.policy.scoring.min_confidence_by_type),
            },
        },
        "experiment": {
            "holdout_fraction": c.experiment.holdout_fraction,
            "experiment_id": c.experiment.experiment_id,
            "horizon_days": c.experiment.horizon_days,
        },
    }


def config_from_dict(data: dict[str, Any]) -> ProductConfig:
    """Reconstruct and validate. Missing `reasons`/`policy`/lists fall back to defaults."""
    reasons_data = data.get("reasons")
    reasons = (
        [ReasonDef(r["id"], r["description"]) for r in reasons_data]
        if reasons_data else list(DEFAULT_REASONS)
    )
    interventions = [
        Intervention(
            id=i["id"], type=i["type"], description=i["description"],
            eligible_when=i.get("eligible_when"),
        )
        for i in data.get("interventions", [])
    ]

    pol = data.get("policy") or {}
    pol_kwargs: dict[str, Any] = {}
    if "confidence_floor" in pol:
        pol_kwargs["confidence_floor"] = pol["confidence_floor"]
    if "preferred" in pol:
        pol_kwargs["preferred"] = {k: list(v) for k, v in pol["preferred"].items()}
    if "discount_reasons" in pol:
        pol_kwargs["discount_reasons"] = set(pol["discount_reasons"])
    if "let_go_reasons" in pol:
        pol_kwargs["let_go_reasons"] = set(pol["let_go_reasons"])
    if "rank_by" in pol:
        pol_kwargs["rank_by"] = pol["rank_by"]
    if "corroboration" in pol:
        pol_kwargs["corroboration"] = dict(pol["corroboration"])
    if "contradiction_penalty" in pol:
        pol_kwargs["contradiction_penalty"] = pol["contradiction_penalty"]
    if "act_confidence" in pol:
        pol_kwargs["act_confidence"] = pol["act_confidence"]
    if "scoring" in pol and pol["scoring"]:
        sc = pol["scoring"]
        sc_kwargs: dict[str, Any] = {}
        for k in ("value_horizon_months", "default_type_cost", "default_save_prior",
                  "default_effectiveness"):
            if k in sc:
                sc_kwargs[k] = sc[k]
        for k in ("type_cost", "save_prior", "type_effectiveness", "min_confidence_by_type"):
            if k in sc:
                sc_kwargs[k] = dict(sc[k])
        pol_kwargs["scoring"] = Scoring(**sc_kwargs)
    policy = Policy(**pol_kwargs)

    ex = data.get("experiment") or {}
    ex_kwargs: dict[str, Any] = {}
    for k in ("holdout_fraction", "experiment_id", "horizon_days"):
        if k in ex:
            ex_kwargs[k] = ex[k]
    experiment = Experiment(**ex_kwargs)

    config = ProductConfig(
        product_name=data["product_name"],
        product_context=data["product_context"],
        activation_definition=data["activation_definition"],
        pricing_summary=data["pricing_summary"],
        known_churn_reasons=list(data.get("known_churn_reasons", [])),
        competitors=list(data.get("competitors", [])),
        interventions=interventions,
        reasons=reasons,
        policy=policy,
        experiment=experiment,
        config_version=str(data.get("config_version", "1")),
    )
    return config.validate()


def config_to_json(c: ProductConfig, *, indent: int = 2) -> str:
    return json.dumps(config_to_dict(c), indent=indent)


def config_from_json(text: str) -> ProductConfig:
    return config_from_dict(json.loads(text))
