from __future__ import annotations

import asyncio
import base64
import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Awaitable, Callable
from urllib.parse import urljoin, urlparse

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

VERSION_RE = re.compile(r"^20\d{2}-\d{2}-\d{2}$")
TRANSPORT_NAMES = {"rest", "mcp", "a2a", "embedded"}
CURRENT_UCP_VERSION = os.getenv("UCP_CURRENT_VERSION", "2026-08-25")


def _b64url_decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def verify_auteric_attestation(attestation: dict, profile_url: str) -> dict[str, Any]:
    """Verify exposure using a Scanner-pinned key, never a key supplied by the profile."""
    invalid = {"status": "invalid", "label": "Auteric attestation invalid"}
    payload, signature = attestation.get("payload"), attestation.get("signature")
    if attestation.get("alg") != "Ed25519" or not isinstance(payload, dict) or not isinstance(signature, str):
        return {**invalid, "reason": "missing or unsupported signed payload"}
    if payload.get("kind") != "auteric.ucp.exposure.v1":
        return {**invalid, "reason": "unsupported attestation kind"}
    profile_host = (urlparse(profile_url).hostname or "").rstrip(".").lower()
    if str(payload.get("domain") or "").rstrip(".").lower() != profile_host:
        return {**invalid, "reason": "attested domain does not match profile host"}
    trusted_key = os.getenv("AUTERIC_ATTESTATION_PUBLIC_KEY", "").strip()
    if not trusted_key:
        return {
            "status": "unverified",
            "label": "Auteric attestation present but not trusted",
            "reason": "Scanner trust key is not configured",
        }
    try:
        key = Ed25519PublicKey.from_public_bytes(_b64url_decode(trusted_key))
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        key.verify(_b64url_decode(signature), encoded)
    except InvalidSignature:
        return {**invalid, "reason": "signature verification failed"}
    except (TypeError, ValueError):
        return {**invalid, "reason": "malformed signing material"}
    return {
        "status": "verified",
        "label": "Auteric exposure verified",
        "detail": (
            "Auteric signed this advertised agent-commerce surface. "
            "This does not prove ordinary storefront traffic is routed through Auteric."
        ),
        "key_id": attestation.get("kid"),
        "capability_count": len(payload.get("capabilities") or []),
        # Safe navigation metadata from the independently verified payload.
        # Scanner never trusts the same value from an unverified declaration.
        "store_id": payload.get("store_id") if isinstance(payload.get("store_id"), str) else None,
    }

CAPABILITY_LABELS = {
    "dev.ucp.shopping.cart": "Cart",
    "dev.ucp.shopping.order": "Order Management",
    "dev.ucp.shopping.checkout": "Checkout",
    "dev.ucp.shopping.discount": "Discount",
    "dev.ucp.shopping.fulfillment": "Fulfillment",
    "dev.ucp.shopping.catalog.lookup": "Catalog Lookup",
    "dev.ucp.shopping.catalog.search": "Catalog Search",
    "dev.shopify.catalog": "Dev Shopify Catalog",
}


def _walk(value: Any):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk(child)


def _strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [str(x) for x in value if isinstance(x, (str, int, float))]
    return []



def _authority_domain(identifier: str) -> str | None:
    parts = [p for p in identifier.lower().split(".") if p]
    if len(parts) < 2:
        return None
    return f"{parts[1]}.{parts[0]}"


def _schema_authority_ok(identifier: str, schema_url: str) -> bool | None:
    authority = _authority_domain(identifier)
    host = (urlparse(schema_url).hostname or "").lower().rstrip(".")
    if not authority or not host:
        return None
    return host == authority or host.endswith("." + authority)


def _registry_entries(registry: Any):
    if not isinstance(registry, dict):
        return
    for name, values in registry.items():
        if isinstance(values, dict):
            values = [values]
        if not isinstance(values, list):
            continue
        for item in values:
            if isinstance(item, dict):
                yield str(name), item


def validate_profile_structure(payload: Any, *, allow_local_http: bool = False) -> dict[str, Any]:
    errors: list[str] = []
    warnings: list[str] = []
    authority: list[dict[str, Any]] = []
    version_mismatches: list[dict[str, str]] = []
    if not isinstance(payload, dict):
        return {"valid": False, "errors": ["Profile root must be an object"], "warnings": [], "authority_bindings": [], "version_mismatches": []}
    ucp = payload.get("ucp")
    if not isinstance(ucp, dict):
        errors.append("Missing object: ucp")
        return {"valid": False, "errors": errors, "warnings": warnings, "authority_bindings": authority, "version_mismatches": version_mismatches}
    version = ucp.get("version")
    if not isinstance(version, str) or not VERSION_RE.match(version):
        errors.append("ucp.version must use YYYY-MM-DD")
    for key in ("services", "capabilities", "payment_handlers"):
        if key not in ucp:
            errors.append(f"Missing required registry: ucp.{key}")
        elif not isinstance(ucp.get(key), dict):
            errors.append(f"ucp.{key} must be an object registry")

    supported_versions = ucp.get("supported_versions")
    supported_release_ids = set(supported_versions) if isinstance(supported_versions, dict) else set()
    for registry_name in ("services", "capabilities", "payment_handlers"):
        registry = ucp.get(registry_name)
        if not isinstance(registry, dict):
            continue
        for name, values in registry.items():
            if not isinstance(values, list):
                errors.append(f"ucp.{registry_name}.{name} must be an array")
                continue
            if not values:
                warnings.append(f"ucp.{registry_name}.{name} is empty")
            for idx, item in enumerate(values):
                if not isinstance(item, dict):
                    errors.append(f"ucp.{registry_name}.{name}[{idx}] must be an object")
                    continue
                item_version = item.get("version")
                if not isinstance(item_version, str) or not VERSION_RE.match(item_version):
                    errors.append(f"ucp.{registry_name}.{name}[{idx}].version is missing or malformed")
                if (
                    str(name).startswith("dev.ucp.")
                    and isinstance(version, str)
                    and item_version
                    and item_version != version
                    and item_version not in supported_release_ids
                ):
                    version_mismatches.append({"name": str(name), "profile_version": version, "declared_version": str(item_version)})
                schema = item.get("schema")
                if isinstance(schema, str):
                    ok = _schema_authority_ok(str(name), schema)
                    authority.append({"name": str(name), "schema": schema, "authority": _authority_domain(str(name)), "valid": ok})
                    if ok is False:
                        errors.append(f"Schema authority mismatch for {name}")
                if registry_name == "services":
                    transport = str(item.get("transport") or "").lower()
                    if transport not in TRANSPORT_NAMES:
                        errors.append(f"Unsupported or missing transport for service {name}")
                    endpoint = item.get("endpoint")
                    local_endpoint = (
                        allow_local_http and isinstance(endpoint, str)
                        and urlparse(endpoint).scheme == "http"
                        and (urlparse(endpoint).hostname or "").lower() in {"localhost", "127.0.0.1", "::1"}
                    )
                    if transport != "embedded" and not (isinstance(endpoint, str) and (endpoint.startswith("https://") or local_endpoint)):
                        errors.append(f"Service {name} must declare an HTTPS endpoint for {transport or 'transport'}")
    if supported_versions is not None:
        if not isinstance(supported_versions, dict):
            errors.append("ucp.supported_versions must be an object map")
        else:
            for supported_version, profile_uri in supported_versions.items():
                if not VERSION_RE.match(str(supported_version)):
                    errors.append(f"ucp.supported_versions contains malformed version: {supported_version}")
                if not (isinstance(profile_uri, str) and profile_uri.startswith("https://")):
                    errors.append(f"ucp.supported_versions.{supported_version} must be an HTTPS profile URI")
                if isinstance(version, str) and str(supported_version) == version:
                    warnings.append("ucp.supported_versions redundantly includes the current version")

    keys = payload.get("keys")
    if keys is not None and not isinstance(keys, list):
        errors.append("Root keys must be an array")
    if isinstance(keys, list):
        for idx, key in enumerate(keys):
            if not isinstance(key, dict):
                errors.append(f"keys[{idx}] must be an object")
                continue
            if not key.get("kid") or not key.get("kty"):
                errors.append(f"keys[{idx}] must include kid and kty")
            kty = str(key.get("kty") or "")
            if kty == "OKP" and not (key.get("crv") and key.get("x")):
                errors.append(f"keys[{idx}] OKP key requires crv and x")
            if kty == "EC" and not (key.get("crv") and key.get("x") and key.get("y")):
                errors.append(f"keys[{idx}] EC key requires crv, x and y")
            if kty == "RSA" and not (key.get("n") and key.get("e")):
                errors.append(f"keys[{idx}] RSA key requires n and e")
    if version_mismatches:
        warnings.append("One or more dev.ucp.* declarations do not match ucp.version")
    return {"valid": not errors, "errors": errors, "warnings": warnings, "authority_bindings": authority, "authority_valid": all(x.get("valid") is not False for x in authority), "version_mismatches": version_mismatches}

def analyze_ucp(payload: Any, profile_url: str) -> dict[str, Any]:
    result: dict[str, Any] = {
        "present": isinstance(payload, dict),
        "json_valid": isinstance(payload, dict),
        "profile_url": profile_url,
        "version": None,
        "version_well_formed": False,
        "current_version": CURRENT_UCP_VERSION,
        "version_current": False,
        "supported_versions": {},
        "capabilities": [],
        "capability_details": [],
        "capability_count": 0,
        "checkout_capability": False,
        "catalog_capability": False,
        "cart_capability": False,
        "order_capability": False,
        "identity_capability": False,
        "transports": [],
        "transport_endpoints": [],
        "payment_handler_count": 0,
        "payment_handlers_declared": False,
        "signing_keys": [],
        "signing_keys_present": False,
        "legacy_signing_keys": False,
        "reference_urls": [],
        "reference_records": [],
        "https_references": True,
        "warnings": [],
        "schema_validation": {"valid": False, "errors": ["Manifest unavailable"], "warnings": []},
        "authority_bindings": [],
        "authority_valid": None,
        "version_mismatches": [],
        "auteric_attestation": {"status": "not_detected", "label": "Auteric attestation not detected"},
    }
    if not isinstance(payload, dict):
        result["warnings"].append("Manifest is not a JSON object")
        return result

    local_test = bool(payload.get("auteric_local_test")) and (urlparse(profile_url).hostname or "").lower() in {"localhost", "127.0.0.1", "::1"}
    result["local_test"] = local_test
    attestation = payload.get("auteric_attestation")
    if isinstance(attestation, dict):
        result["auteric_attestation"] = verify_auteric_attestation(attestation, profile_url)

    validation = validate_profile_structure(payload, allow_local_http=local_test)
    result["schema_validation"] = validation
    result["authority_bindings"] = validation.get("authority_bindings", [])
    result["authority_valid"] = validation.get("authority_valid")
    result["version_mismatches"] = validation.get("version_mismatches", [])

    ucp = payload.get("ucp") if isinstance(payload.get("ucp"), dict) else payload
    version = ucp.get("version") if isinstance(ucp, dict) else None
    if isinstance(version, str):
        result["version"] = version
        result["version_well_formed"] = bool(VERSION_RE.match(version))
    result["version_current"] = bool(result["version"] == CURRENT_UCP_VERSION)
    supported_versions = ucp.get("supported_versions") if isinstance(ucp, dict) else None
    if isinstance(supported_versions, dict):
        result["supported_versions"] = {str(k): str(v) for k, v in supported_versions.items() if isinstance(v, str)}
    if not result["version_well_formed"]:
        result["warnings"].append("UCP version is missing or not date-formatted")
    elif not result["version_current"]:
        result["warnings"].append(f"Profile uses {result['version']}; scanner current release is {CURRENT_UCP_VERSION}")

    capabilities: set[str] = set()
    cap_obj = ucp.get("capabilities") if isinstance(ucp, dict) else None
    if isinstance(cap_obj, dict):
        capabilities.update(str(k) for k in cap_obj.keys())
        for name, entry in _registry_entries(cap_obj):
            result["capability_details"].append({
                "id": name,
                "label": CAPABILITY_LABELS.get(name, name.rsplit(".", 1)[-1].replace("_", " ").title()),
                "version": entry.get("version"),
                "kind": "extension" if entry.get("extends") or not name.startswith("dev.ucp.") else "core",
                "extends": [str(value) for value in entry.get("extends", []) if isinstance(value, str)] if isinstance(entry.get("extends"), list) else [],
                "schema_authority_valid": _schema_authority_ok(name, entry.get("schema")) if isinstance(entry.get("schema"), str) else None,
                "schema_declared": isinstance(entry.get("schema"), str),
            })
    elif isinstance(cap_obj, list):
        for item in cap_obj:
            if isinstance(item, str):
                capabilities.add(item)
            elif isinstance(item, dict):
                name = item.get("name") or item.get("id") or item.get("capability")
                if name:
                    capabilities.add(str(name))

    for node in _walk(payload):
        for key in ("capability", "capability_id"):
            if isinstance(node.get(key), str):
                capabilities.add(node[key])
    caps = sorted(capabilities)
    result["capabilities"] = caps
    result["capability_count"] = len(caps)
    lower_caps = " ".join(caps).lower()
    result["checkout_capability"] = "checkout" in lower_caps
    result["catalog_capability"] = "catalog" in lower_caps
    result["cart_capability"] = "cart" in lower_caps
    result["order_capability"] = "order" in lower_caps
    result["identity_capability"] = "identity" in lower_caps

    transports: set[str] = set()
    endpoints: list[dict[str, str]] = []
    refs: set[str] = set()
    for node in _walk(payload):
        # transport can be a string, list, or object; detect conservatively.
        for key in ("transport", "type", "protocol"):
            value = node.get(key)
            for raw in _strings(value):
                name = raw.lower()
                if name in TRANSPORT_NAMES:
                    transports.add(name)
                    endpoint = node.get("endpoint") or node.get("url") or node.get("uri")
                    if isinstance(endpoint, str) and endpoint.startswith(("http://", "https://")):
                        endpoints.append({"transport": name, "url": endpoint})
        for key in ("transports",):
            value = node.get(key)
            if isinstance(value, list):
                for item in value:
                    if isinstance(item, str) and item.lower() in TRANSPORT_NAMES:
                        transports.add(item.lower())
                    elif isinstance(item, dict):
                        name = str(item.get("type") or item.get("transport") or item.get("protocol") or "").lower()
                        if name in TRANSPORT_NAMES:
                            transports.add(name)
                            endpoint = item.get("endpoint") or item.get("url") or item.get("uri")
                            if isinstance(endpoint, str) and endpoint.startswith(("http://", "https://")):
                                endpoints.append({"transport": name, "url": endpoint})
        for key in ("spec", "schema", "endpoint", "url", "uri"):
            value = node.get(key)
            if isinstance(value, str) and value.startswith(("http://", "https://")):
                refs.add(urljoin(profile_url, value))

    reference_records: list[dict[str, Any]] = []
    for registry_name in ("services", "capabilities", "payment_handlers"):
        registry = ucp.get(registry_name) if isinstance(ucp, dict) else None
        for name, entry in _registry_entries(registry):
            for kind in ("schema", "spec"):
                raw_url = entry.get(kind)
                if isinstance(raw_url, str) and raw_url.startswith(("http://", "https://")):
                    resolved = urljoin(profile_url, raw_url)
                    reference_records.append({
                        "kind": kind, "registry": registry_name, "name": name,
                        "version": entry.get("version"), "url": resolved,
                        "authority_valid": _schema_authority_ok(name, resolved) if kind == "schema" else None,
                    })
                    refs.add(resolved)
    for supported_version, supported_uri in (result.get("supported_versions") or {}).items():
        if supported_uri.startswith(("http://", "https://")):
            reference_records.append({"kind": "profile", "registry": "supported_versions", "name": supported_version, "version": supported_version, "url": supported_uri})
            refs.add(supported_uri)
    unique_records = {(x.get("kind"), x.get("name"), x.get("url")): x for x in reference_records}
    result["reference_records"] = list(unique_records.values())

    # de-duplicate endpoint records.
    uniq = {(x["transport"], x["url"]): x for x in endpoints}
    result["transports"] = sorted(transports)
    result["transport_endpoints"] = list(uniq.values())
    result["reference_urls"] = sorted(refs)
    result["https_references"] = all(urlparse(u).scheme == "https" for u in refs) if refs else True
    if not result["https_references"]:
        result["warnings"].append("One or more UCP references use HTTP")

    payment_handlers = ucp.get("payment_handlers") if isinstance(ucp, dict) else None
    if payment_handlers is None:
        payment_handlers = payload.get("payment_handlers")
    result["payment_handlers_declared"] = payment_handlers is not None
    if isinstance(payment_handlers, dict):
        result["payment_handler_count"] = len(payment_handlers)
    elif isinstance(payment_handlers, list):
        result["payment_handler_count"] = len(payment_handlers)

    keys = payload.get("keys")
    legacy = payload.get("signing_keys")
    result["legacy_signing_keys"] = bool(legacy)
    selected = keys if isinstance(keys, list) else (legacy if isinstance(legacy, list) else [])
    valid_keys = []
    for item in selected:
        if isinstance(item, dict):
            valid_keys.append({k: item.get(k) for k in ("kid", "kty", "alg", "crv", "use") if item.get(k) is not None})
    result["signing_keys"] = valid_keys
    result["signing_keys_present"] = bool(valid_keys)
    if result["legacy_signing_keys"] and not isinstance(keys, list):
        result["warnings"].append("Legacy signing_keys field detected; canonical profiles use root-level keys")

    return result


def _protocol_grade(score: int) -> str:
    if score >= 90:
        return "A"
    if score >= 80:
        return "B"
    if score >= 70:
        return "C"
    if score >= 55:
        return "D"
    return "F"


def build_protocol_summary(
    analysis: dict[str, Any],
    acp_analysis: dict[str, Any] | None = None,
    *,
    transaction_tests: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build a public-safe protocol assessment without claiming runtime behavior."""
    ucp_detected = bool(analysis.get("present") and analysis.get("json_valid"))
    structural = analysis.get("schema_validation") or {}
    official = analysis.get("official_schema_validation") or {}
    schema_ok = official.get("valid") if official.get("ran") and official.get("valid") is not None else structural.get("valid")
    transport_probes = analysis.get("transport_probes") or []
    transport_liveness = (
        all(bool(row.get("reachable")) for row in transport_probes)
        if transport_probes
        else (True if analysis.get("transports") == ["embedded"] else None)
    )
    local_test = bool(analysis.get("local_test"))
    criteria = [
        ("profile", "Publish a valid UCP profile", ucp_detected, 10),
        ("content_type", "Serve the manifest as JSON", analysis.get("content_type_json"), 5),
        ("version", "Use the current well-formed UCP release", bool(analysis.get("version_well_formed") and analysis.get("version_current")), 10),
        ("schema", "Conform to the release schema", schema_ok, 15),
        ("authority", "Bind schemas to their namespace authority", analysis.get("authority_valid"), 10),
        ("versions", "Keep registry versions compatible", not bool(analysis.get("version_mismatches")), 5),
        ("capabilities", "Declare commerce capabilities", bool(analysis.get("capability_count")), 15),
        ("checkout", "Declare checkout support", bool(analysis.get("checkout_capability")), 10),
        ("transports", "Declare an agent transport", bool(analysis.get("transports")), 5),
        ("payments", "Declare payment handlers", bool(analysis.get("payment_handlers_declared")), 5),
        ("transport_liveness", "Expose a reachable agent transport", transport_liveness, 3),
    ]
    if not local_test:
        criteria.append(("keys", "Publish keys at the manifest root", bool(analysis.get("signing_keys_present")), 7))
    earned = sum(weight for _, _, value, weight in criteria if value is True)
    score = max(0, min(100, round(earned))) if ucp_detected else 0
    blockers = [
        {"id": key, "title": title, "weight": weight, "status": "fail" if value is False else "unknown"}
        for key, title, value, weight in sorted(criteria, key=lambda row: row[3], reverse=True)
        if value is not True
    ]

    probes_by_capability: dict[tuple[str, str | None], dict[str, Any]] = {}
    for probe in analysis.get("reference_probes") or []:
        if probe.get("registry") == "capabilities" and probe.get("kind") == "schema":
            probes_by_capability[(str(probe.get("name")), probe.get("version"))] = probe
    capability_details = []
    seen: set[tuple[str, str | None]] = set()
    for detail in analysis.get("capability_details") or []:
        key = (str(detail.get("id")), detail.get("version"))
        if key in seen:
            continue
        seen.add(key)
        probe = probes_by_capability.get(key) or {}
        authority_ok = detail.get("schema_authority_valid")
        if authority_ok is False or probe.get("ok") is False:
            binding = "failed"
        elif authority_ok is True and probe.get("ok") is True:
            binding = "bound"
        elif detail.get("schema_declared"):
            binding = "declared"
        else:
            binding = "not_declared"
        capability_details.append({**detail, "binding": binding})

    acp = acp_analysis or {"detected": False, "status": "not_detected"}
    transports = [str(value).upper() for value in analysis.get("transports") or []]
    if ucp_detected:
        interface_label = f"UCP over {' + '.join(transports)}" if transports else "UCP (transport not declared)"
        commerce_protocol = "UCP"
    elif acp.get("detected"):
        interface_label = f"ACP over {str(acp.get('interface') or 'REST').upper()}"
        commerce_protocol = "ACP"
    else:
        interface_label = "No standardized commerce interface detected"
        commerce_protocol = "None detected"

    executed = [row for row in (transaction_tests or []) if isinstance(row, dict) and row.get("status") in {"pass", "fail", "warning"}]
    attestation = analysis.get("auteric_attestation") or {}
    exposure_verified = attestation.get("status") == "verified"
    enforcement_verified = bool(executed)
    runtime = {
        "auteric_detected": exposure_verified or enforcement_verified,
        "exposure_verified": exposure_verified,
        "enforcement_verified": enforcement_verified,
        "status": "enforcement_verified" if enforcement_verified else "exposure_verified" if exposure_verified else "not_detected",
        "label": (
            "Auteric enforcement verified" if enforcement_verified else
            "Auteric exposure verified" if exposure_verified else
            "Auteric runtime not detected"
        ),
        "detail": (
            f"{len(executed)} connected runtime controls produced evidence."
            if enforcement_verified
            else attestation.get("detail")
            if exposure_verified
            else "No connected Auteric enforcement evidence was available to this public scan."
        ),
        "transaction_tests_executed": len(executed),
    }
    return {
        "commerce_protocol": commerce_protocol,
        "agent_interface": interface_label,
        "ucp": {
            "detected": ucp_detected,
            "version": analysis.get("version"),
            "conformance_score": score,
            "grade": _protocol_grade(score),
            "assessment": "automated_public",
            "capabilities": capability_details,
            "capability_count": len(capability_details),
            "transports": analysis.get("transports") or [],
            "biggest_blockers": blockers[:3],
            "reference_advisories": sum(1 for row in analysis.get("reference_probes") or [] if not row.get("ok")),
        },
        "acp": acp,
        "runtime": runtime,
    }


async def probe_acp(
    root_url: str,
    safe_request: Callable[..., Awaitable[Any]],
    client: Any,
    *,
    page_html: str = "",
) -> dict[str, Any]:
    """Look for explicit public ACP evidence without invoking a checkout mutation."""
    rows: list[dict[str, Any]] = []
    detected = False
    interface = None
    source = None
    page_lower = page_html.lower()
    if any(marker in page_lower for marker in ("agentic commerce protocol", "agenticcommerce.dev", "dev.acp.")):
        detected, source = True, "storefront_markup"

    candidates = [
        ("GET", urljoin(root_url, "/.well-known/acp")),
        ("GET", urljoin(root_url, "/.well-known/acp.json")),
        ("OPTIONS", urljoin(root_url, "/checkout_sessions")),
    ]
    for method, url in candidates:
        try:
            response = await safe_request(client, method, url, accept="application/json,*/*")
            body = response.text[:4_000].lower()
            headers = {str(key).lower(): str(value) for key, value in response.headers.items()}
            explicit_marker = any(marker in body for marker in ("agentic commerce protocol", "dev.acp.", "\"acp_version\""))
            official_link = "agenticcommerce.dev" in headers.get("link", "").lower()
            checkout_signature = method == "OPTIONS" and "api-version" in headers and "checkout" in body
            matched = response.status_code < 500 and (explicit_marker or official_link or checkout_signature)
            rows.append({"method": method, "path": urlparse(url).path, "status": response.status_code, "matched": matched})
            if matched and not detected:
                detected = True
                source = urlparse(url).path
                interface = "rest" if "checkout_sessions" in url else "declared"
        except Exception as exc:
            rows.append({"method": method, "path": urlparse(url).path, "status": None, "matched": False, "error": str(exc)[:120]})
    return {
        "detected": detected,
        "status": "detected" if detected else "not_detected" if any(row.get("status") in {200, 204, 404, 405} for row in rows) else "unable_to_verify",
        "interface": interface,
        "source": source,
        "probes": rows,
        "note": "ACP has no standardized public discovery manifest; only explicit public declarations and non-mutating endpoint signals were evaluated.",
    }


async def probe_references(
    analysis: dict[str, Any],
    safe_get: Callable[..., Awaitable[Any]],
    client: Any,
    *,
    limit: int = 16,
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    profile_url = analysis.get("profile_url")
    records = analysis.get("reference_records") or [{"kind": "reference", "url": u} for u in (analysis.get("reference_urls") or [])]
    seen: set[str] = set()
    selected: list[dict[str, Any]] = []
    ordered_records = sorted(
        records,
        key=lambda row: (
            0 if row.get("registry") == "capabilities" and row.get("kind") == "schema" else
            1 if row.get("registry") == "capabilities" and row.get("kind") == "spec" else
            2 if row.get("kind") == "schema" else
            3
        ),
    )
    for record in ordered_records:
        url = record.get("url")
        if not url or url == profile_url or url in seen or len(selected) >= limit:
            continue
        seen.add(url)
        selected.append(record)

    semaphore = asyncio.Semaphore(6)

    async def _probe(record: dict[str, Any]) -> dict[str, Any]:
        url = record.get("url")
        row = {k: record.get(k) for k in ("kind", "registry", "name", "version", "url", "authority_valid") if record.get(k) is not None}
        if row.get("kind") == "schema" and row.get("authority_valid") is False:
            row.update({"status": None, "ok": False, "fetched": False, "error": "Schema authority binding failed; URL was not fetched"})
            return row
        try:
            async with semaphore:
                # UCP schema URLs are provenance-sensitive. Do not accept redirects for schemas.
                if row.get("kind") == "schema":
                    response = await safe_get(client, url, accept="application/schema+json,application/json,*/*", follow_redirects=False)
                else:
                    response = await safe_get(client, url, accept="application/json,text/plain,text/html,*/*")
            row.update({"status": response.status_code, "ok": response.status_code < 400, "fetched": True})
            if row.get("kind") == "schema" and response.status_code < 400:
                try:
                    schema_payload = response.json()
                    embedded_name = schema_payload.get("name") if isinstance(schema_payload, dict) else None
                    embedded_version = schema_payload.get("version") if isinstance(schema_payload, dict) else None
                    row["embedded_name"] = embedded_name
                    row["embedded_version"] = embedded_version
                    row["identity_matches"] = (embedded_name in {None, row.get("name")}) and (embedded_version in {None, row.get("version")})
                    if row["identity_matches"] is False:
                        row["ok"] = False
                        row["error"] = "Fetched schema identity does not match registry declaration"
                except Exception:
                    row["schema_json"] = False
        except Exception as exc:
            row.update({"status": None, "ok": False, "fetched": False, "error": str(exc)[:180]})
        return row

    if selected:
        out.extend(await asyncio.gather(*(_probe(record) for record in selected)))
    return out


async def validate_with_official_ucp_schema(payload: Any) -> dict[str, Any]:
    """Use the official ucp-schema CLI when installed; never imply it ran when absent."""
    binary = shutil.which(os.getenv("UCP_SCHEMA_BIN", "ucp-schema"))
    version = payload.get("ucp", {}).get("version") if isinstance(payload, dict) and isinstance(payload.get("ucp"), dict) else None
    result: dict[str, Any] = {
        "engine": "ucp-schema", "available": bool(binary), "ran": False, "valid": None,
        "version": version, "schema": None, "errors": [],
    }
    if not binary or not isinstance(version, str) or not VERSION_RE.match(version):
        return result
    schema_url = f"https://ucp.dev/{version}/schemas/ucp.json"
    result["schema"] = schema_url

    def _run() -> dict[str, Any]:
        path = None
        try:
            with tempfile.NamedTemporaryFile("w", suffix=".json", encoding="utf-8", delete=False) as fh:
                # business_schema validates the inner UCP business object. Passing the
                # discovery envelope would incorrectly report version/services missing.
                json.dump(payload.get("ucp") if isinstance(payload.get("ucp"), dict) else payload, fh)
                path = fh.name
            cmd = [binary, "validate", path, "--schema", schema_url, "--response", "--op", "read", "--def", "business_schema", "--json"]
            timeout_seconds = max(1.0, min(10.0, float(os.getenv("UCP_SCHEMA_TIMEOUT_SECONDS", "4"))))
            completed = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_seconds, check=False)
            parsed = None
            try:
                parsed = json.loads(completed.stdout) if completed.stdout.strip() else None
            except Exception:
                parsed = None
            errors = parsed.get("errors", []) if isinstance(parsed, dict) else []
            if not errors and completed.returncode != 0:
                errors = [completed.stderr.strip()[:500] or f"ucp-schema exited with {completed.returncode}"]
            return {
                "engine": "ucp-schema", "available": True, "ran": True,
                "valid": completed.returncode == 0 and (not isinstance(parsed, dict) or parsed.get("valid", True) is True),
                "version": version, "schema": schema_url, "errors": errors, "exit_code": completed.returncode,
            }
        except subprocess.TimeoutExpired:
            return {**result, "available": True, "ran": True, "valid": None, "errors": ["ucp-schema validation timed out"]}
        except Exception as exc:
            return {**result, "available": True, "ran": True, "valid": None, "errors": [str(exc)[:500]]}
        finally:
            if path:
                try:
                    Path(path).unlink(missing_ok=True)
                except Exception:
                    pass

    return await asyncio.to_thread(_run)


async def probe_transports(
    analysis: dict[str, Any],
    safe_request: Callable[..., Awaitable[Any]],
    client: Any,
    *,
    limit: int = 8,
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for item in (analysis.get("transport_endpoints") or [])[:limit]:
        transport = item.get("transport")
        url = item.get("url")
        if not url or transport == "embedded":
            continue
        try:
            if transport == "mcp":
                response = await safe_request(
                    client,
                    "POST",
                    url,
                    accept="application/json,*/*",
                    headers={"Content-Type": "application/json"},
                    json_body={"jsonrpc": "2.0", "id": "auteric-scan-tools", "method": "tools/list", "params": {}},
                )
                body = response.text[:500].lower()
                protocol_ok = response.status_code < 500 and ("result" in body or "tools" in body or response.status_code in {200, 400, 401, 403, 405})
            else:
                response = await safe_request(client, "OPTIONS", url, accept="application/json,*/*")
                protocol_ok = response.status_code < 500
            out.append({"transport": transport, "url": url, "status": response.status_code, "reachable": protocol_ok})
        except Exception as exc:
            out.append({"transport": transport, "url": url, "status": None, "reachable": False, "error": str(exc)[:180]})
    return out


def _find_product_list(value: Any, *, limit: int = 20) -> list[dict[str, Any]]:
    """Find a bounded product array inside common MCP/UCP response envelopes."""
    if isinstance(value, dict):
        for key in ("products", "items", "results"):
            candidate = value.get(key)
            if isinstance(candidate, list) and candidate and all(isinstance(x, dict) for x in candidate[:3]):
                return candidate[:limit]
        # MCP structured content and nested result envelopes.
        for key in ("structuredContent", "structured_content", "data", "result", "content"):
            if key in value:
                found = _find_product_list(value.get(key), limit=limit)
                if found:
                    return found
        for child in value.values():
            found = _find_product_list(child, limit=limit)
            if found:
                return found
    elif isinstance(value, list):
        for child in value[:12]:
            if isinstance(child, dict) and child.get("type") == "text" and isinstance(child.get("text"), str):
                try:
                    decoded = json.loads(child["text"])
                except Exception:
                    decoded = None
                if decoded is not None:
                    found = _find_product_list(decoded, limit=limit)
                    if found:
                        return found
            found = _find_product_list(child, limit=limit)
            if found:
                return found
    return []


def _catalog_shape(products: list[dict[str, Any]]) -> dict[str, Any]:
    sample = products[:20]
    total = len(sample)
    def present(product: dict[str, Any], *keys: str) -> bool:
        return any(product.get(k) not in (None, "", [], {}) for k in keys)
    variant_products = [p for p in sample if isinstance(p.get("variants"), list)]
    variant_rows = [v for p in variant_products for v in (p.get("variants") or []) if isinstance(v, dict)]
    return {
        "sampled_products": total,
        "title": sum(1 for p in sample if present(p, "title", "name")),
        "description": sum(1 for p in sample if present(p, "description")),
        "image": sum(1 for p in sample if present(p, "image", "images", "image_url", "imageUrl")),
        "price": sum(1 for p in sample if present(p, "price", "offers")),
        "currency": sum(1 for p in sample if present(p, "currency", "price_currency", "priceCurrency") or (isinstance(p.get("price"), dict) and bool(p["price"].get("currency")))),
        "availability": sum(1 for p in sample if present(p, "availability", "available")),
        "variants_products": len(variant_products),
        "variant_rows": len(variant_rows),
        "variant_ids": sum(1 for v in variant_rows if present(v, "id", "variant_id", "sku")),
        "variant_price": sum(1 for v in variant_rows if present(v, "price")),
        "variant_availability": sum(1 for v in variant_rows if present(v, "availability", "available")),
    }


async def probe_catalog(
    analysis: dict[str, Any],
    safe_request: Callable[..., Awaitable[Any]],
    client: Any,
    *,
    agent_profile_url: str | None = None,
) -> dict[str, Any]:
    """Run one benign MCP catalog.search-style call when a suitable read-only tool is advertised."""
    if not analysis.get("catalog_capability"):
        return {"attempted": False, "reason": "catalog capability not declared"}
    mcp = next((x for x in (analysis.get("transport_endpoints") or []) if x.get("transport") == "mcp" and x.get("url")), None)
    if not mcp:
        return {"attempted": False, "reason": "no callable MCP endpoint declared"}
    if not agent_profile_url or not agent_profile_url.startswith("https://"):
        return {"attempted": False, "reason": "scanner agent profile URL is not configured as public HTTPS"}
    url = mcp["url"]
    try:
        tools_response = await safe_request(
            client, "POST", url, accept="application/json,*/*", headers={"Content-Type": "application/json"},
            json_body={"jsonrpc": "2.0", "id": "auteric-catalog-tools", "method": "tools/list", "params": {}},
        )
        tools_json = tools_response.json() if tools_response.content else {}
        tools = []
        if isinstance(tools_json, dict):
            tools = ((tools_json.get("result") or {}).get("tools") or tools_json.get("tools") or [])
        names = [str(t.get("name")) for t in tools if isinstance(t, dict) and t.get("name")]
        preferred = [n for n in names if n.lower() in {"search_catalog", "catalog_search", "search-catalog", "catalog.search"}]
        if not preferred:
            preferred = [n for n in names if "catalog" in n.lower() and "search" in n.lower()]
        if not preferred:
            return {"attempted": False, "reason": "no read-only catalog search tool advertised", "tools": names[:30], "endpoint": url}
        tool = preferred[0]
        arguments = {
            "meta": {"ucp-agent": {"profile": agent_profile_url}},
            "catalog": {
                "filters": {},
                "context": {"language": "en"},
                "pagination": {"limit": 20},
            },
        }
        call = await safe_request(
            client, "POST", url, accept="application/json,*/*", headers={"Content-Type": "application/json"},
            json_body={"jsonrpc": "2.0", "id": "auteric-catalog-search", "method": "tools/call", "params": {"name": tool, "arguments": arguments}},
        )
        body = call.json() if call.content else {}
        products = _find_product_list(body, limit=20)
        shape = _catalog_shape(products)
        ok = call.status_code < 400 and bool(products)
        return {
            "attempted": True,
            "ok": ok,
            "transport": "mcp",
            "endpoint": url,
            "tool": tool,
            "http_status": call.status_code,
            "agent_profile_url": agent_profile_url,
            "request_shape": {"meta": True, "catalog": True, "pagination_limit": 20},
            "product_count": len(products),
            "shape": shape,
            "error": None if ok else ((body.get("error") or {}).get("message") if isinstance(body, dict) else None) or "Catalog search returned no parseable product records",
        }
    except Exception as exc:
        return {"attempted": True, "ok": False, "transport": "mcp", "endpoint": url, "error": str(exc)[:220]}
