from __future__ import annotations

from typing import Any

from models import Capability, SecurityCheck


RISK_SPECS = [
    {
        "id": "business.intent_integrity",
        "title": "Will the final AI order match what the shopper approved?",
        "short_title": "Final order matches approval",
        "controls": [
            "transaction.checkout_lock_mutation",
            "transaction.amount_sku_mutation",
            "transaction.toctou",
        ],
        "impact": "A changed amount, quantity, SKU, variant, address or shipping choice can turn an approved purchase into a different order.",
        "protected_copy": "Connected tests verified that approved transaction terms stay bound through execution.",
        "unknown_copy": "The public scan cannot prove that the final order stays bound to the shopper's original approval.",
        "failed_copy": "A connected transaction test showed that approved transaction terms can change before execution.",
        "cta": "Protect AI checkout",
    },
    {
        "id": "business.duplicate_actions",
        "title": "Can the same AI action run twice?",
        "short_title": "Duplicate actions prevented",
        "controls": [
            "transaction.replay",
            "transaction.idempotency_collision",
            "transaction.retry_double_debit",
        ],
        "impact": "Retries or replayed requests can create duplicate orders, repeated side effects, support costs or double-payment risk.",
        "protected_copy": "Connected tests verified replay, idempotency and retry protections for the tested transaction path.",
        "unknown_copy": "The public scan cannot verify whether repeated or replayed AI requests are safely deduplicated.",
        "failed_copy": "A connected test accepted a replay, idempotency collision or unsafe retry behavior.",
        "cta": "Prevent duplicate AI actions",
    },
    {
        "id": "business.agent_identity",
        "title": "Can you prove which agent acted, for whom and under what approval?",
        "short_title": "Agent identity is attributable",
        "controls": [
            "transaction.identity_binding",
            "transaction.message_signature_tamper",
        ],
        "impact": "Without strong attribution, an incident can leave the merchant unable to prove which agent, shopper or authorization caused the action.",
        "protected_copy": "Connected identity and signature tests verified attribution on the tested transaction path.",
        "unknown_copy": "The public scan cannot independently verify end-to-end agent identity and authorization binding for transactions.",
        "failed_copy": "A connected identity or signature test showed that the authorization boundary can be confused or transferred.",
        "cta": "Verify agent identity",
    },
]


def _status_for(checks_by_id: dict[str, SecurityCheck], ids: list[str]) -> tuple[str, list[SecurityCheck]]:
    rows = [checks_by_id[x] for x in ids if x in checks_by_id]
    if any(x.status == "fail" for x in rows):
        return "confirmed_gap", rows
    if any(x.status == "warning" for x in rows):
        return "needs_attention", rows
    if rows and all(x.status == "pass" for x in rows):
        return "protected", rows
    return "not_verified", rows


def _capability_supported(capabilities: dict[str, Capability | dict[str, Any]], name: str) -> bool:
    raw = capabilities.get(name)
    if raw is None:
        return False
    if isinstance(raw, Capability):
        return bool(raw.supported)
    if isinstance(raw, dict):
        return bool(raw.get("supported"))
    return False


def build_business_risks(
    checks: list[SecurityCheck],
    capabilities: dict[str, Capability | dict[str, Any]],
    observations: dict[str, Any],
) -> dict[str, Any]:
    """Translate technical controls into merchant-readable business risk.

    This layer never converts an unexecuted transaction test into a vulnerability. Unknown
    means exactly that: the public scanner cannot prove the protection without a connected
    runtime/staging path.
    """
    by_id = {x.id: x for x in checks}
    items: list[dict[str, Any]] = []

    for spec in RISK_SPECS:
        status, rows = _status_for(by_id, spec["controls"])
        if status == "protected":
            summary = spec["protected_copy"]
            status_label = "Verified protection"
        elif status == "confirmed_gap":
            summary = spec["failed_copy"]
            status_label = "Confirmed gap"
        elif status == "needs_attention":
            summary = spec["unknown_copy"]
            status_label = "Needs attention"
        else:
            summary = spec["unknown_copy"]
            status_label = "Protection not verified"

        items.append({
            "id": spec["id"],
            "title": spec["title"],
            "short_title": spec["short_title"],
            "status": status,
            "status_label": status_label,
            "summary": summary,
            "business_impact": spec["impact"],
            "cta": spec["cta"],
            "controls": [
                {"id": row.id, "title": row.title, "status": row.status}
                for row in rows
            ],
        })

    runtime_supported = _capability_supported(capabilities, "runtime_protection")
    tx_checks = [x for x in checks if x.category == "transaction"]
    tx_known = [x for x in tx_checks if x.status in {"pass", "fail", "warning"}]
    if runtime_supported and tx_known:
        runtime_status = "protected" if not any(x.status == "fail" for x in tx_known) else "confirmed_gap"
        runtime_label = "Runtime protection connected" if runtime_status == "protected" else "Runtime protection needs attention"
        runtime_summary = "A connected runtime is present and transaction controls produced evidence." if runtime_status == "protected" else "A connected runtime is present, but at least one transaction control failed."
    elif runtime_supported:
        runtime_status = "not_verified"
        runtime_label = "Runtime detected, protection not verified"
        runtime_summary = "A runtime component was detected, but availability alone is not treated as transaction protection."
    else:
        runtime_status = "not_verified"
        runtime_label = "Runtime enforcement not verified"
        runtime_summary = "The public scan cannot verify whether a private enforcement layer inspects and stops unsafe AI actions in real time."

    items.append({
        "id": "business.runtime_enforcement",
        "title": "Is there a control point that can stop an unsafe AI action before checkout?",
        "short_title": "Runtime protection",
        "status": runtime_status,
        "status_label": runtime_label,
        "summary": runtime_summary,
        "business_impact": "Detection without an inline decision point cannot block a changed, duplicate or unauthorized transaction before the merchant side effect happens.",
        "cta": "Add runtime protection",
        "controls": [{"id": "runtime_protection", "title": "Enforcement Gateway", "status": "pass" if runtime_supported else "unknown"}],
    })

    confirmed = sum(1 for x in items if x["status"] == "confirmed_gap")
    attention = sum(1 for x in items if x["status"] == "needs_attention")
    unverified = sum(1 for x in items if x["status"] == "not_verified")
    protected = sum(1 for x in items if x["status"] == "protected")

    if confirmed:
        headline = f"{confirmed} AI transaction protection gap{'s' if confirmed != 1 else ''} confirmed"
        narrative = "Address the confirmed transaction gap first, then verify the remaining runtime boundaries."
    elif unverified or attention:
        headline = "Your AI storefront is visible. Transaction protection is not fully verified."
        narrative = "The public scan can validate the storefront and protocol surface, but only a connected control point can prove what happens between shopper approval and final execution."
    else:
        headline = "AI transaction protection is verified for the tested controls"
        narrative = "Keep monitoring changes so the verified transaction boundary does not drift over time."

    return {
        "headline": headline,
        "narrative": narrative,
        "items": items,
        "counts": {
            "confirmed_gap": confirmed,
            "needs_attention": attention,
            "not_verified": unverified,
            "protected": protected,
        },
        "gateway_recommended": bool(confirmed or attention or unverified),
        "gateway_cta": "Protect my store",
        "technical_note": "AP2, message signatures, replay tokens, JWKS and related protocol details stay in Technical Evidence; the overview translates them into business outcomes.",
    }
