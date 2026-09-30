from __future__ import annotations

from typing import Any

from models import SecurityCheck

# Public attack-path labels are intentionally non-operational: they explain impact without
# turning the scanner report into an exploitation playbook.
PUBLIC_ATTACKS = {
    "transport.https": ("Traffic interception / request tampering", "A network attacker may be able to observe or alter agent/store traffic before it reaches the merchant."),
    "transport.tls_certificate": ("Merchant impersonation risk", "Clients may be unable to establish a trustworthy merchant identity when certificate validation fails."),
    "web.csp": ("Browser script-injection blast radius", "A weak or missing CSP can increase the impact of a separate script-injection flaw."),
    "web.cors": ("Cross-origin data exposure", "An overly permissive CORS policy may let an untrusted origin read responses that should stay origin-bound."),
    "ucp.keys": ("Merchant signatures cannot be independently verified", "The public UCP profile does not publish verification keys for merchant-signed material through this discovery path."),
    "ucp.authority": ("Schema / capability authority confusion", "A capability can reference trust material outside the authority expected for its namespace."),
    "agent.https_references": ("UCP reference downgrade", "Protocol references over plaintext HTTP can weaken integrity of discovery and schema material."),
    "agent.transport": ("Declared transport reliability gap", "Agents may be routed to a declared transport that does not behave as advertised."),
}

LOCKED_ATTACKS = [
    ("Order changed after shopper approval", "Verify the final amount, product, quantity and other purchase terms still match what the shopper approved."),
    ("Duplicate AI actions", "Verify retries and repeated authorized requests cannot create the same order or side effect twice."),
    ("Who actually authorized the action?", "Verify an approval cannot be transferred to a different shopper, agent or session."),
    ("Protection outage / fail-open", "Verify an unsafe transaction is stopped if the security enforcement dependency becomes unavailable."),
    ("Same approval, different transaction", "Verify the same request identity cannot be reused for different purchase details."),
    ("Checkout changed before completion", "Verify product, quantity, address and shipping state cannot change after approval without a new authorization."),
    ("Protocol downgrade", "Verify protected actions cannot fall back from a signed route to a weaker path."),
    ("TOCTOU / merchant-state drift", "Verify authoritative price, inventory and policy state is rechecked immediately before execution."),
    ("Duplicate charge / order retry", "Verify retries remain idempotent at the final payment and order side effect."),
    ("Amount / SKU substitution", "Verify economic terms at execution exactly match the user-approved transaction."),
    ("Stale authorization reuse", "Verify expired or stale signed authorization material cannot be reused."),
    ("Content-Digest tampering", "Verify body integrity is cryptographically bound to the authorized request."),
]


def build_attack_surface(checks: list[SecurityCheck]) -> dict[str, Any]:
    public: list[dict[str, Any]] = []
    for check in checks:
        if check.status not in {"fail", "warning"}:
            continue
        mapped = PUBLIC_ATTACKS.get(check.id)
        if not mapped:
            continue
        title, impact = mapped
        public.append({
            "id": f"attack.{check.id}",
            "title": title,
            "severity": check.severity,
            "status": "confirmed_gap" if check.status == "fail" else "needs_review",
            "summary": impact,
            "control": check.title,
            "evidence": check.evidence,
            "verified_by": check.method,
            "visibility": "public",
        })

    # A clean public scan can still show what the connected gateway tests, but not pretend
    # those attacks were executed. Keep only a concise preview and never return hidden methods/evidence.
    locked = [
        {
            "id": f"gateway.attack.{idx+1}",
            "title": title,
            "summary": summary,
            "visibility": "locked",
            "status": "requires_connected_protection",
            "service_required": "Auteric Protection (planned)",
        }
        for idx, (title, summary) in enumerate(LOCKED_ATTACKS)
    ]
    return {
        "public": public[:6],
        "locked": locked,
        "public_count": len(public),
        "locked_count": len(locked),
        "gateway": {
            "name": "Auteric Protection (planned)",
            "components": ["Merchant authorization", "Checkout policy enforcement"],
            "promise": "Planned merchant-authorized controls. No transaction protection is active in this deployment.",
        },
    }
