from __future__ import annotations

from typing import Any

from models import AdapterResult, SecurityCheck


TRANSACTION_TESTS = [
    {
        "id": "transaction.message_signature_tamper",
        "title": "Message signature tampering",
        "method": "Modify a signed transaction-critical request and verify the merchant rejects the changed message.",
        "severity": "critical",
        "exposure": "A modified checkout or payment request could be accepted after it was signed.",
        "recommendation": "Verify HTTP Message Signatures over every transaction-critical field before processing the request.",
    },
    {
        "id": "transaction.content_digest_tamper",
        "title": "Content-Digest tampering",
        "method": "Change the request body while keeping the original Content-Digest/signature evidence.",
        "severity": "critical",
        "exposure": "An attacker may be able to change the request body without invalidating integrity checks.",
        "recommendation": "Require and validate Content-Digest and bind it to the signed request.",
    },
    {
        "id": "transaction.stale_signature",
        "title": "Signature freshness",
        "method": "Replay a correctly signed request outside the accepted freshness window.",
        "severity": "high",
        "exposure": "Old authorization material could remain usable longer than intended.",
        "recommendation": "Enforce created/expires timestamps and a narrow clock-skew window.",
    },
    {
        "id": "transaction.replay",
        "title": "Signed request replay",
        "method": "Repeat the same signed transaction request and verify it cannot create a second side effect.",
        "severity": "critical",
        "exposure": "A captured valid request could be replayed to duplicate a transaction or side effect.",
        "recommendation": "Bind nonce/replay tokens to the principal and transaction and reject reuse.",
    },
    {
        "id": "transaction.idempotency_collision",
        "title": "Idempotency key collision",
        "method": "Reuse an idempotency key with a different request body and verify the merchant denies the collision.",
        "severity": "critical",
        "exposure": "The same idempotency key could authorize a different transaction body.",
        "recommendation": "Persist a hash of the original request and reject key reuse with different content.",
    },
    {
        "id": "transaction.identity_binding",
        "title": "Identity binding after signing",
        "method": "Attempt to change the user/agent principal after the request has been signed or approved.",
        "severity": "critical",
        "exposure": "Authorization could be transferred to a different identity after approval.",
        "recommendation": "Bind principal identity, session, mandate and signature to the same immutable transaction context.",
    },
    {
        "id": "transaction.protocol_downgrade",
        "title": "Signed-to-unsigned protocol downgrade",
        "method": "Attempt the same protected action through an unsigned or weaker protocol path.",
        "severity": "critical",
        "exposure": "Attackers may bypass signed flows by falling back to an unprotected route.",
        "recommendation": "Enforce the strongest required protocol at the authorization boundary and reject downgrade attempts.",
    },
    {
        "id": "transaction.checkout_lock_mutation",
        "title": "Checkout lock mutation",
        "method": "Change SKU, quantity, address or shipping state after checkout approval/lock.",
        "severity": "critical",
        "exposure": "Approved checkout state could be changed before execution.",
        "recommendation": "Hash and lock approved checkout state; require re-approval after any material mutation.",
    },
    {
        "id": "transaction.toctou",
        "title": "Merchant-state TOCTOU",
        "method": "Change merchant-side state between authorization and execution and verify the server re-validates it.",
        "severity": "high",
        "exposure": "Price, inventory or policy state may change after authorization without a final integrity check.",
        "recommendation": "Re-read and compare authoritative merchant state immediately before execution.",
    },
    {
        "id": "transaction.amount_sku_mutation",
        "title": "Amount and SKU integrity",
        "method": "Mutate price, currency, SKU or quantity between approval and execution.",
        "severity": "critical",
        "exposure": "A transaction could execute with different economic terms than the user or agent approved.",
        "recommendation": "Bind amount, currency, items and quantities to the authorization and verify them at execution.",
    },
    {
        "id": "transaction.retry_double_debit",
        "title": "Retry / double-debit protection",
        "method": "Retry an already accepted completion and verify the payment/order side effect remains singular.",
        "severity": "critical",
        "exposure": "Network retries could cause duplicate charges or orders.",
        "recommendation": "Use durable idempotency around the final side effect and return the original result on exact retries.",
    },
    {
        "id": "transaction.fail_closed",
        "title": "Fail-closed enforcement",
        "method": "Make the policy/security dependency unavailable and verify protected execution is denied rather than bypassed.",
        "severity": "critical",
        "exposure": "A security component outage could silently disable transaction protection.",
        "recommendation": "Fail closed for protected actions and expose an explicit recoverable error path.",
    },
]


def _check(
    cid: str,
    category: str,
    title: str,
    method: str,
    status: str,
    severity: str,
    exposure: str,
    result: str,
    evidence: Any,
    recommendation: str | None = None,
    *,
    confidence: float = 0.92,
    source: str = "external_scan",
) -> SecurityCheck:
    return SecurityCheck(
        id=cid,
        category=category,
        title=title,
        method=method,
        status=status,
        severity=severity,
        exposure=exposure,
        result=result,
        evidence=evidence,
        recommendation=recommendation,
        confidence=confidence,
        source=source,
    )


def _from_bool(
    cid: str,
    category: str,
    title: str,
    method: str,
    value: bool | None,
    severity: str,
    fail_exposure: str,
    evidence: Any,
    recommendation: str,
    *,
    fail_status: str = "fail",
    pass_result: str = "Control verified",
    unknown_result: str = "Could not verify from the available scan surface",
    source: str = "external_scan",
) -> SecurityCheck:
    if value is True:
        return _check(cid, category, title, method, "pass", "info", "No exposure observed by this test.", pass_result, evidence, source=source, confidence=0.96)
    if value is False:
        return _check(cid, category, title, method, fail_status, severity, fail_exposure, "Control did not meet the scanner requirement", evidence, recommendation, source=source)
    return _check(cid, category, title, method, "unknown", severity, "Exposure is unknown because this control could not be verified.", unknown_result, evidence, recommendation, source=source, confidence=0.72)


def build_checks(adapter_result: AdapterResult) -> list[SecurityCheck]:
    o = adapter_result.observations or {}
    products = adapter_result.products or []
    checks: list[SecurityCheck] = []

    # Transport / browser security.
    https = o.get("https") if isinstance(o.get("https"), bool) else None
    checks.append(_from_bool(
        "transport.https", "transport", "HTTPS transport",
        "Verify the effective storefront URL uses HTTPS.", https, "critical",
        "Traffic can be observed or modified in transit before it reaches the merchant.",
        {"https": https, "target": adapter_result.target_url},
        "Serve the storefront only over HTTPS and redirect HTTP to HTTPS.",
    ))

    tls = o.get("tls") or {}
    cert_value = tls.get("certificate_valid") if tls.get("tested") else None
    checks.append(_from_bool(
        "transport.tls_certificate", "transport", "TLS certificate validation",
        "Perform a verified TLS handshake and validate certificate trust/expiry.", cert_value, "critical",
        "Clients may be unable to authenticate the merchant endpoint or may be exposed to interception.", tls,
        "Repair certificate validity, hostname coverage and the certificate chain.",
    ))
    modern_tls = None if not tls.get("tested") or tls.get("error") else tls.get("version") in {"TLSv1.2", "TLSv1.3"}
    checks.append(_from_bool(
        "transport.tls_version", "transport", "Modern TLS protocol",
        "Inspect the negotiated TLS protocol version.", modern_tls, "high",
        "Legacy TLS may expose weaker protocol behavior or compatibility downgrade risk.", tls,
        "Disable legacy TLS and require TLS 1.2 or newer.", fail_status="warning",
    ))

    redirect = o.get("http_redirect") or {}
    redirect_ok = redirect.get("redirects_to_https") if redirect.get("tested") else None
    checks.append(_from_bool(
        "transport.http_redirect", "transport", "HTTP to HTTPS redirect",
        "Request the HTTP origin without following redirects and verify it points directly to HTTPS.", redirect_ok, "medium",
        "Users or agents may accidentally begin navigation over plaintext HTTP.", redirect,
        "Return a permanent redirect from HTTP to the canonical HTTPS origin.", fail_status="warning",
    ))

    h = o.get("security_header_analysis") or {}
    hc = h.get("controls") or {}
    csp = h.get("csp") or {}
    header_specs = [
        ("web.hsts", "Strong HSTS", "Check Strict-Transport-Security and require a meaningful max-age.", hc.get("hsts_strong"), "medium", "Returning clients remain more exposed to HTTPS downgrade attempts.", h.get("hsts") or {}, "Enable HSTS after HTTPS coverage is complete; use a long max-age and consider includeSubDomains.", "warning"),
        ("web.csp", "Content Security Policy", "Check for an active Content-Security-Policy response header.", hc.get("csp"), "high", "Injected browser content has fewer restrictions and XSS impact may be higher.", csp, "Deploy a restrictive CSP and prefer nonces/hashes for scripts.", "warning"),
        ("web.csp_eval", "CSP blocks unsafe-eval", "Inspect the active CSP for unsafe-eval.", hc.get("csp_no_unsafe_eval") if hc.get("csp") else None, "high", "Dynamic code evaluation weakens browser script execution controls.", csp, "Remove unsafe-eval and migrate affected script execution patterns.", "warning"),
        ("web.frame_protection", "Clickjacking protection", "Verify X-Frame-Options or CSP frame-ancestors is present.", hc.get("frame_protection"), "medium", "The storefront may be embeddable in an attacker-controlled frame for clickjacking.", h.get("raw") or {}, "Set CSP frame-ancestors and/or an appropriate X-Frame-Options fallback.", "warning"),
        ("web.nosniff", "MIME sniffing protection", "Verify X-Content-Type-Options is set to nosniff.", hc.get("nosniff"), "medium", "Browsers may infer content types in ways that increase script/content confusion risk.", h.get("raw") or {}, "Set X-Content-Type-Options: nosniff.", "warning"),
        ("web.referrer", "Referrer-Policy", "Verify an explicit Referrer-Policy header is present.", hc.get("referrer_policy"), "low", "Navigation may leak more source URL information than intended.", h.get("raw") or {}, "Set a privacy-preserving Referrer-Policy such as strict-origin-when-cross-origin.", "warning"),
        ("web.permissions", "Permissions-Policy", "Verify an explicit Permissions-Policy header is present.", hc.get("permissions_policy"), "low", "Browser capabilities may be available more broadly than the application requires.", h.get("raw") or {}, "Restrict unnecessary browser capabilities with Permissions-Policy.", "warning"),
    ]
    for cid, title, method, value, severity, exposure, evidence, recommendation, fail_status in header_specs:
        checks.append(_from_bool(cid, "web", title, method, value, severity, exposure, evidence, recommendation, fail_status=fail_status))

    cookies = o.get("cookie_security") or {}
    cookie_issues = cookies.get("sensitive_cookie_issues") if isinstance(cookies.get("sensitive_cookie_issues"), list) else cookies.get("sensitive_without_secure_httponly")
    cookie_safe = None if cookies.get("count") is None else not bool(cookie_issues)
    checks.append(_from_bool(
        "web.cookie_flags", "web", "Sensitive cookie hardening",
        "Inspect Set-Cookie values. Session/auth/token/checkout cookies require Secure + HttpOnly; cart-state cookies are not penalized for HttpOnly alone.", cookie_safe, "high",
        "One or more sensitive-looking cookies were returned without the hardening expected for their observed role. This is a heuristic warning; inspect the named cookie and reasons in Evidence before remediation.", cookies,
        "Add Secure to sensitive cookies and HttpOnly to cookies that do not need JavaScript access; keep an explicit SameSite policy.", fail_status="warning",
    ))

    cors = (o.get("cors") or {}).get("home") or {}
    cors_safe = None if cors.get("status") is None else not bool(cors.get("wildcard_credentials") or cors.get("reflects_untrusted_origin"))
    checks.append(_from_bool(
        "web.cors", "web", "CORS rejects an untrusted origin",
        "Send a safe OPTIONS preflight from attacker.invalid and inspect reflected/wildcard credential behavior.", cors_safe, "high",
        "An untrusted web origin may be able to read credentialed application responses.", cors,
        "Use a strict CORS allowlist and never reflect arbitrary credentialed origins.",
    ))

    mixed = (o.get("page_signals") or {}).get("mixed_content_urls")
    mixed_safe = None if mixed is None else not bool(mixed)
    checks.append(_from_bool(
        "web.mixed_content", "web", "No mixed HTTP content",
        "Inspect scripts, images, stylesheets and frames for http:// subresources on an HTTPS page.", mixed_safe, "medium",
        "HTTP subresources can weaken page integrity and may be blocked or modified in transit.", {"mixed_content_urls": mixed or []},
        "Serve every subresource over HTTPS.", fail_status="warning",
    ))

    disclosure = h.get("server_disclosure") or {}
    disclosed = bool(disclosure.get("server") or disclosure.get("x_powered_by"))
    checks.append(_from_bool(
        "web.server_disclosure", "web", "Minimal server fingerprinting",
        "Inspect Server and X-Powered-By headers for unnecessary technology disclosure.", not disclosed, "low",
        "Technology/version hints can make opportunistic targeting easier.", disclosure,
        "Remove unnecessary Server/X-Powered-By details at the edge or application server.", fail_status="warning",
    ))

    security_txt = (o.get("standard_files") or {}).get("security_txt") or {}
    security_txt_value = security_txt.get("present") if "present" in security_txt else None
    checks.append(_from_bool(
        "web.security_txt", "web", "security.txt disclosure channel",
        "Request /.well-known/security.txt and verify a non-empty security contact document exists.", security_txt_value, "low",
        "Researchers may have no standardized channel for reporting security issues.", security_txt,
        "Publish /.well-known/security.txt with a monitored security contact and policy links.", fail_status="warning",
    ))

    # Catalog / AI-commerce surface.
    total = len(products)
    image_count = sum(1 for p in products if p.images)
    price_count = sum(1 for p in products if p.price is not None)
    structured_count = sum(1 for p in products if p.structured_data)
    checks.append(_from_bool(
        "catalog.products", "catalog", "Public product discovery",
        "Discover products from Product JSON-LD, Shopify/Woo public endpoints or bounded storefront product cards.", bool(total), "medium",
        "Agents may be forced to scrape presentation HTML and can miss sellable products.", {"products": total},
        "Publish machine-readable product data or connect the commerce platform.", fail_status="warning",
    ))
    checks.append(_from_bool(
        "catalog.images", "catalog", "Product image coverage",
        "Verify discovered products expose at least one usable image URL.", None if total == 0 else image_count == total, "low",
        "Products without images are harder for agents and users to identify, compare and validate.", {"products": total, "with_images": image_count},
        "Expose a canonical HTTPS product image in structured data or the commerce API.", fail_status="warning",
    ))
    checks.append(_from_bool(
        "catalog.prices", "catalog", "Product price coverage",
        "Verify discovered products expose machine-readable price values.", None if total == 0 else price_count == total, "medium",
        "Agents may quote incomplete or stale commercial terms when price data is missing.", {"products": total, "with_price": price_count},
        "Expose current price and currency for every purchasable product.", fail_status="warning",
    ))
    checks.append(_from_bool(
        "catalog.structured", "catalog", "Structured product data",
        "Verify Product JSON-LD or an authoritative commerce API backs the discovered catalog.", None if total == 0 else structured_count > 0 or adapter_result.platform in {"shopify", "woocommerce"}, "low",
        "Catalog interpretation depends more heavily on fragile HTML scraping.", {"products": total, "structured_products": structured_count, "platform": adapter_result.platform},
        "Publish Product JSON-LD and/or expose an authoritative product API.", fail_status="warning",
    ))

    availability_count = sum(1 for p in products if p.availability)
    identity_count = sum(1 for p in products if p.sku or p.gtin)
    variant_products = [p for p in products if p.variants]
    variant_resolvable = None
    if variant_products:
        variant_resolvable = all(
            all(bool(v.id) and bool(v.title) and (v.price is not None or p.price is not None) for v in p.variants)
            for p in variant_products
        )
    checks.append(_from_bool(
        "catalog.availability", "catalog", "Product availability coverage",
        "Verify each sampled product exposes a machine-readable stock/availability state.", None if total == 0 else availability_count == total, "medium",
        "Agents may recommend or attempt to buy products without a verified stock state.", {"products": total, "with_availability": availability_count},
        "Expose availability for each product/variant in the authoritative catalog response.", fail_status="warning",
    ))
    checks.append(_from_bool(
        "catalog.identity", "catalog", "Stable product identity",
        "Verify sampled products expose a merchant SKU or global product identifier.", None if total == 0 else identity_count == total, "low",
        "Agents have weaker item identity when mapping discovery results into cart/checkout actions.", {"products": total, "with_sku_or_gtin": identity_count},
        "Expose stable SKU/variant IDs and GTIN/barcode where applicable.", fail_status="warning",
    ))
    checks.append(_from_bool(
        "catalog.variants", "catalog", "Variant resolvability",
        "When variants are exposed, verify each variant has a stable ID/title and resolvable price context.", variant_resolvable, "medium",
        "An agent may discover a product but fail to select the intended sellable variant.", {"products_with_variants": len(variant_products)},
        "Expose stable variant identifiers, option labels, pricing and availability for each sellable variant.", fail_status="warning",
    ))

    checks.append(_from_bool(
        "catalog.descriptions", "catalog", "Product description coverage",
        "Verify sampled products expose a useful machine-readable description.", None if total == 0 else sum(1 for p in products if p.description and len(p.description.strip()) >= 40) == total, "low",
        "Agents have less semantic context for matching, comparison and product explanation.", {"products": total, "with_description": sum(1 for p in products if p.description and len(p.description.strip()) >= 40)},
        "Expose a useful product description in the authoritative catalog response.", fail_status="warning",
    ))
    checks.append(_from_bool(
        "catalog.currency", "catalog", "Price currency coverage",
        "Verify every sampled product with a price also exposes an ISO-style currency code.", None if total == 0 or price_count == 0 else all((p.price is None) or bool(p.currency) for p in products), "medium",
        "Agents can misquote or compare prices incorrectly when the currency is ambiguous.", {"products": total, "with_price": price_count, "with_price_and_currency": sum(1 for p in products if p.price is not None and p.currency)},
        "Return a currency code alongside every machine-readable price.", fail_status="warning",
    ))
    checks.append(_from_bool(
        "catalog.canonical", "catalog", "Canonical product links",
        "Verify every sampled product has a stable product-detail URL.", None if total == 0 else all(bool(p.url) for p in products), "low",
        "Agents have a weaker canonical target for attribution, re-fetching and user handoff.", {"products": total, "with_url": sum(1 for p in products if p.url)},
        "Expose a stable canonical URL for every product.", fail_status="warning",
    ))

    ucp_catalog = ((o.get("ucp_analysis") or {}).get("catalog_probe") or {})
    catalog_cap = bool((o.get("ucp_analysis") or {}).get("catalog_capability"))
    if catalog_cap:
        live_value = ucp_catalog.get("ok") if ucp_catalog.get("attempted") else None
        checks.append(_from_bool(
            "catalog.ucp_search", "catalog", "Live UCP catalog search",
            "Discover a read-only catalog search tool from MCP and run one benign search query.", live_value, "medium",
            "The manifest declares catalog discovery, but a live agent may not receive usable product results.", ucp_catalog,
            "Repair the declared catalog search tool/transport and return typed product records.", fail_status="fail",
        ))
        shape = ucp_catalog.get("shape") or {}
        sampled = int(shape.get("sampled_products") or 0)
        shape_ok = None if not ucp_catalog.get("attempted") or not sampled else (
            int(shape.get("title") or 0) == sampled and int(shape.get("price") or 0) > 0 and int(shape.get("image") or 0) > 0
        )
        checks.append(_from_bool(
            "catalog.ucp_shape", "catalog", "UCP catalog response completeness",
            "Audit the live catalog-search sample for product identity, price and image fields.", shape_ok, "medium",
            "Catalog search results may be technically reachable but too incomplete for reliable agent shopping.", shape or ucp_catalog,
            "Return complete typed catalog records with identity, price/currency, images, availability and variant data.", fail_status="warning",
        ))

    # UCP checker parity.
    u = o.get("ucp_analysis") or {}
    ucp_status = o.get("ucp_status")
    ucp_present = ucp_status == "verified" and bool(u.get("json_valid"))
    checks.append(_from_bool(
        "ucp.profile", "ucp", "UCP business profile",
        "Request /.well-known/ucp and parse it as a UCP JSON business profile.", True if ucp_present else False if ucp_status in {"not_found", "invalid"} else None, "high",
        "AI commerce agents cannot rely on a standardized UCP discovery contract for this merchant.", {"status": ucp_status, "profile_url": u.get("profile_url")},
        "Publish a valid JSON UCP profile at /.well-known/ucp.", fail_status="warning",
    ))

    if ucp_present:
        official = u.get("official_schema_validation") or {}
        structural = u.get("schema_validation") or {}
        official_value = official.get("valid") if official.get("ran") else None
        checks.append(_from_bool(
            "ucp.official_schema", "ucp", "Official UCP schema validation",
            "Validate the profile with the official ucp-schema engine for the declared UCP release.", official_value, "high",
            "Release-specific schema violations may break strict UCP clients.", official,
            "Fix the reported official schema errors and re-run validation.",
        ))
        structural_value = structural.get("valid") if isinstance(structural, dict) else None
        checks.append(_from_bool(
            "ucp.structural_schema", "ucp", "UCP structural validation",
            "Check required profile structure, registry fields and authority constraints even when the official validator is unavailable.", structural_value, "high",
            "The profile structure is inconsistent with the scanner's UCP contract checks.", structural,
            "Correct the profile structure and registry declarations.",
        ))

        refs = u.get("reference_probes") or []
        transports = u.get("transport_probes") or []
        ref_ok = None if not refs else all(bool(x.get("ok")) for x in refs)
        transport_ok = None if not transports else all(bool(x.get("reachable")) for x in transports)
        bots = o.get("ai_bot_access") or {}
        all_bots = None if not bots else all(bool(v) for v in bots.values())
        standard = o.get("standard_files") or {}
        page = o.get("page_signals") or {}

        ucp_specs = [
            ("ucp.content_type", "UCP JSON content type", "Verify the UCP profile is served with a JSON content type.", u.get("content_type_json"), "medium", "Strict clients may reject or mishandle the profile response.", {"content_type": u.get("content_type")}, "Serve application/json for /.well-known/ucp.", "fail"),
            ("ucp.version", "Well-formed UCP version", "Validate the declared UCP version format.", u.get("version_well_formed"), "high", "Clients may not be able to resolve the correct release schema.", {"version": u.get("version")}, "Use a valid published UCP release identifier.", "fail"),
            ("ucp.current", "Current UCP release", "Compare the declared version with the scanner's configured current UCP release.", u.get("version_current"), "low", "The merchant may miss newer interoperability and trust-layer behavior.", {"version": u.get("version")}, "Review and upgrade to the current supported UCP release when compatible.", "warning"),
            ("ucp.authority", "Schema authority binding", "Verify namespaced registry entries only fetch schemas from the authorized namespace origin.", u.get("authority_valid"), "critical", "A malicious or compromised profile could point agents at an untrusted schema for a trusted namespace.", {"authority_issues": u.get("authority_issues") or []}, "Serve schemas from the authority bound to the capability/service namespace.", "fail"),
            ("ucp.version_consistency", "Registry version consistency", "Compare capability/service/payment registry versions with the profile release.", not bool(u.get("version_mismatches")), "medium", "Agents may compose incompatible protocol definitions.", {"version_mismatches": u.get("version_mismatches") or []}, "Align registry entries with compatible supported versions.", "warning"),
            ("ucp.capabilities", "Declared capabilities", "Enumerate UCP capabilities and verify the registry is non-empty.", bool(u.get("capability_count")), "medium", "Agents have no standardized capability contract to plan commerce actions.", {"capability_count": u.get("capability_count"), "capabilities": u.get("capabilities")}, "Declare the merchant's supported UCP capabilities.", "warning"),
            ("ucp.checkout", "Checkout capability", "Detect a declared UCP checkout capability.", bool(u.get("checkout_capability")), "medium", "Agents cannot rely on a standardized checkout capability.", {"checkout_capability": u.get("checkout_capability")}, "Declare and implement the UCP checkout capability if agent checkout is supported.", "warning"),
            ("ucp.transports", "Declared transports", "Extract declared REST/MCP/A2A/Embedded transport bindings.", bool(u.get("transports")), "medium", "Agents have no declared protocol transport to call.", {"transports": u.get("transports")}, "Declare at least one supported UCP transport binding.", "warning"),
            ("ucp.payments", "Payment handlers", "Enumerate declared UCP payment handlers.", bool(u.get("payment_handlers_declared")), "medium", "Payment capability discovery is incomplete or ambiguous.", {"payment_handler_count": u.get("payment_handler_count")}, "Declare supported payment handlers in the UCP profile.", "warning"),
            ("ucp.keys", "Merchant signing keys", "Fetch /.well-known/ucp, inspect root keys/signing_keys, and verify usable public key metadata is published.", bool(u.get("signing_keys_present")), "high", "The public UCP profile does not publish verification keys that agents can use to independently verify merchant-signed material through this discovery path.", {"keys": u.get("signing_keys") or [], "profile_checked": True}, "Publish canonical root-level UCP signing keys and document safe key rotation.", "fail"),
            ("ucp.references", "Declared UCP references", "Resolve declared spec/schema/profile URLs with bounded fetches after authority validation; schemas do not accept redirects.", ref_ok, "high", "One or more declared UCP references could not be validated. Agents following the affected declaration may fail or receive inconsistent protocol metadata.", refs, "Repair the exact broken UCP reference shown in Evidence and keep schema identity aligned with the declaration.", "fail"),
            ("ucp.transport_liveness", "UCP transport liveness", "Probe declared transports safely; MCP uses read-only tools/list.", transport_ok, "high", "Declared agent endpoints may be unreachable or not speaking the expected protocol.", transports, "Repair declared transport endpoints and protocol handling.", "fail"),
            ("ucp.ai_bot_access", "AI crawler access", "Evaluate robots.txt access for major AI crawler user-agents.", all_bots, "low", "Some AI discovery/indexing systems may be blocked from public merchant content.", bots, "Review robots.txt and intentionally allow or block each AI crawler based on policy.", "warning"),
            ("ucp.llms_txt", "llms.txt", "Request /llms.txt and verify it is present.", bool((standard.get("llms_txt") or {}).get("present")), "low", "AI systems have less explicit machine-readable guidance about the site.", standard.get("llms_txt") or {}, "Publish /llms.txt if it is part of your agent discovery strategy.", "warning"),
            ("ucp.sitemap", "Sitemap", "Request /sitemap.xml and verify it is present.", bool((standard.get("sitemap") or {}).get("present")), "low", "Crawlers and discovery agents may have less complete URL coverage.", standard.get("sitemap") or {}, "Publish and keep an XML sitemap current.", "warning"),
            ("ucp.open_graph", "Open Graph metadata", "Inspect the homepage for core Open Graph metadata.", bool(page.get("open_graph")), "low", "Shared/discovered merchant pages may have weaker descriptive metadata.", page, "Publish consistent og:title, og:url/type and related metadata.", "warning"),
            ("ucp.organization_jsonld", "Organization JSON-LD", "Inspect JSON-LD for Organization/Store/OnlineStore identity.", bool(page.get("organization_jsonld")), "low", "Merchant identity is less explicit to structured-data consumers.", page, "Publish Organization/Store JSON-LD with canonical merchant identity fields.", "warning"),
            ("ucp.mobile_viewport", "Mobile viewport", "Verify the storefront declares a responsive viewport meta tag.", bool(page.get("mobile_viewport")), "low", "The storefront may render poorly in mobile or embedded agent/user flows.", page, "Add a responsive viewport meta tag and test narrow layouts.", "warning"),
        ]
        for cid, title, method, value, severity, exposure, evidence, recommendation, fail_status in ucp_specs:
            checks.append(_from_bool(cid, "ucp", title, method, value, severity, exposure, evidence, recommendation, fail_status=fail_status))

        # Agent trust is the security-focused subset of UCP.
        checks.append(_from_bool(
            "agent.https_references", "agent", "HTTPS-only UCP references",
            "Ensure every declared UCP external reference uses HTTPS.", u.get("https_references"), "high",
            "An agent could fetch protocol metadata over an unauthenticated plaintext channel.", {"reference_urls": u.get("reference_urls") or []},
            "Use HTTPS for every UCP reference and endpoint.",
        ))
        checks.append(_from_bool(
            "agent.transport", "agent", "Agent transport reachability",
            "Run non-destructive liveness probes against declared agent transport endpoints.", transport_ok, "high",
            "Agents may discover a capability they cannot actually invoke.", transports,
            "Make declared transport endpoints reachable and protocol-correct.",
        ))
        cors_ucp = (o.get("cors") or {}).get("ucp") or {}
        ucp_cors_safe = None if cors_ucp.get("status") is None else not bool(cors_ucp.get("wildcard_credentials") or cors_ucp.get("reflects_untrusted_origin"))
        checks.append(_from_bool(
            "agent.ucp_cors", "agent", "UCP endpoint CORS boundary",
            "Send a safe preflight from an untrusted origin to the UCP profile endpoint.", ucp_cors_safe, "high",
            "Browser-based malicious origins may gain cross-origin access to agent discovery data or future authenticated endpoints.", cors_ucp,
            "Restrict cross-origin access to the UCP surface according to the intended client model.",
        ))
    else:
        # Keep downstream UCP/agent controls explicit but unknown rather than inventing pass/fail.
        checks.append(_check(
            "agent.surface", "agent", "Agent trust surface", "Evaluate cryptographic keys, references and transport liveness from a verified UCP profile.",
            "unknown", "medium", "Agent trust controls cannot be evaluated without a verified UCP profile.", "UCP profile unavailable", {"ucp_status": ucp_status},
            "Publish or repair the UCP profile before evaluating downstream agent trust controls.", confidence=0.84,
        ))

    # Connected transaction attack suite. Presence of a runtime service alone never counts as a pass.
    observed_tx = o.get("transaction_tests") or []
    by_id: dict[str, dict[str, Any]] = {}
    if isinstance(observed_tx, list):
        for row in observed_tx:
            if isinstance(row, dict) and row.get("id"):
                rid = str(row["id"])
                by_id[rid] = row
                if not rid.startswith("transaction."):
                    by_id[f"transaction.{rid}"] = row

    for spec in TRANSACTION_TESTS:
        observed = by_id.get(spec["id"])
        if observed:
            status = str(observed.get("status") or "unknown")
            if status not in {"pass", "fail", "warning", "unknown", "unsupported"}:
                status = "unknown"
            checks.append(_check(
                spec["id"], "transaction", spec["title"], spec["method"], status,
                "info" if status == "pass" else spec["severity"],
                "No exposure observed by this connected test." if status == "pass" else str(observed.get("exposure") or spec["exposure"]),
                str(observed.get("result") or ("Connected test passed" if status == "pass" else "Connected test did not pass")),
                observed.get("evidence") if "evidence" in observed else observed,
                str(observed.get("recommendation") or spec["recommendation"]),
                confidence=float(observed.get("confidence") or 0.98),
                source="runtime",
            ))
        else:
            checks.append(_check(
                spec["id"], "transaction", spec["title"], spec["method"], "unknown", spec["severity"],
                "Exposure is unknown because this transaction attack was not executed against a connected staging/runtime surface.",
                "Not verified by the public scan", {"tested": False}, spec["recommendation"], confidence=0.99,
            ))

    local_test = (o.get("local_auteric") or {}).get("status") == "connected"
    if local_test:
        local_only = {
            "transport.https", "transport.tls_certificate", "transport.tls_version",
            "web.hsts", "ucp.keys", "agent.https_references",
        }
        for check in checks:
            if check.id in local_only:
                check.status = "unsupported"
                check.result = "Not assessed in local development"
                check.exposure = "This public-production control is assessed after HTTPS publication."
                check.recommendation = "Publish the store on HTTPS, then re-run the public Scanner."
    return checks
