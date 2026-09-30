from __future__ import annotations

import html
import json
from dataclasses import dataclass


@dataclass(frozen=True)
class MarketingPage:
    path: str
    title: str
    description: str
    eyebrow: str
    heading: str
    intro: str
    sections: tuple[tuple[str, str], ...]
    bullets: tuple[str, ...]
    note: str | None = None


PAGES: dict[str, MarketingPage] = {
    "ai-shopping-readiness": MarketingPage(
        "/ai-shopping-readiness",
        "AI Shopping Readiness Checker | Auteric",
        "Check whether AI shopping agents can discover, understand and safely reach the purchase path on your store.",
        "AI SHOPPING READINESS",
        "Can AI shoppers find and understand your store?",
        "Auteric combines public discovery, catalog quality, protocol readiness, checkout signals and security into one evidence-based store scan.",
        (
            ("What is an AI shopping readiness scan?", "It is a non-destructive review of the public signals an AI shopping system can use to discover products, interpret their commercial details and understand available commerce capabilities."),
            ("Why readiness is broader than SEO", "Search visibility helps an agent find a page. Readiness also depends on reliable product identity, price, availability, structured data, protocol declarations and a discoverable purchase path."),
            ("How Auteric reports uncertainty", "Every check is labelled as detected, tested, not detected or unable to verify. Missing evidence is not presented as a confirmed vulnerability."),
        ),
        ("AI crawler and robots.txt access", "Sitemaps, metadata and structured product data", "Catalog completeness and product identity", "UCP availability and safe protocol checks", "Checkout signals without making a purchase"),
    ),
    "chatgpt-shopping": MarketingPage(
        "/chatgpt-shopping",
        "ChatGPT Shopping Readiness Checker | Auteric",
        "See whether your product catalog is prepared for discovery and interpretation by ChatGPT and other AI shopping experiences.",
        "CHATGPT SHOPPING",
        "Is your store ready for ChatGPT shopping?",
        "A public scan shows whether your storefront publishes the product and discovery signals that AI shopping systems may depend on. It does not guarantee placement in any third-party experience.",
        (
            ("Can ChatGPT find my products?", "No outside scanner can guarantee inclusion. Auteric checks the public foundations: crawler access, product pages, structured data, canonical URLs, prices, availability and catalog consistency."),
            ("What commonly gets in the way?", "Blocked crawling, incomplete product data, unstable URLs and unclear availability make product interpretation less reliable across AI and search systems."),
            ("What happens after the scan?", "You receive a prioritized report written for merchants, with technical evidence available only where public disclosure is appropriate."),
        ),
        ("Product discoverability", "Machine-readable price and availability", "Canonical product identity", "Catalog quality across all discoverable pages", "Safe checkout-readiness signals"),
    ),
    "shopify-ai-shopping": MarketingPage(
        "/shopify-ai-shopping",
        "Shopify AI Shopping Readiness Checker | Auteric",
        "Scan a Shopify storefront for AI product discovery, catalog quality, UCP readiness, checkout signals and security.",
        "SHOPIFY AI SHOPPING",
        "Is your Shopify store ready for AI shopping?",
        "Start with a free public storefront scan. If Shopify is detected, Protect my store becomes the path from public evidence to merchant-authorized checkout enforcement.",
        (
            ("Free public scan", "Checks the same storefront evidence available to an unauthenticated AI shopper, including public catalog data, product pages and discovery files."),
            ("Protect my store", "A merchant-authorized Shopify connection can turn findings into policy for spending limits, agent identity, delegated authority and checkout integrity."),
            ("Enforcement boundary", "A Shopify validation function can block checkout when a server-side rule fails. Auteric must still supply the trusted identity, authority and policy decision that the function enforces."),
        ),
        ("Public catalog pagination", "Product titles, variants, SKUs and availability", "Agent identity and delegated authority policy", "Spending and transaction-integrity controls", "Server-side checkout validation"),
        "Shopify installation is not connected in this deployment yet. The free public scan is available today and never claims that private enforcement was tested.",
    ),
    "woocommerce-ai-shopping": MarketingPage(
        "/woocommerce-ai-shopping",
        "WooCommerce AI Shopping Readiness Checker | Auteric",
        "Check a WooCommerce store for AI shopping visibility, catalog structure, protocol readiness and safe checkout signals.",
        "WOOCOMMERCE AI SHOPPING",
        "Prepare WooCommerce for AI shopping agents.",
        "The public scanner inspects discoverable storefront and catalog evidence without credentials or write actions. A connected plugin path can add deeper merchant-authorized checks later.",
        (
            ("What the public scan can see", "Public product data, structured markup, crawler guidance, protocol endpoints, browser security and observable checkout signals."),
            ("What requires store access", "Private configuration, authoritative inventory and detailed security validation require an authenticated owner connection."),
            ("No real purchase", "Auteric does not submit an order, capture payment or mutate inventory during a public scan."),
        ),
        ("WooCommerce platform detection", "Paginated public catalog review", "Product field coverage", "Protocol and crawler discovery", "Non-destructive security checks"),
        "A merchant-authorized WooCommerce integration is planned. No plugin installation is required for the public scan.",
    ),
    "ucp-validator": MarketingPage(
        "/ucp-validator",
        "UCP Validator & Security Scanner | Auteric",
        "Validate a store's public UCP profile, schema, references, transports, catalog response and trust signals.",
        "UNIVERSAL COMMERCE PROTOCOL",
        "UCP validator and security scanner",
        "Auteric fetches the public UCP profile, validates the declared structure and safely tests related discovery surfaces where the merchant exposes them.",
        (
            ("What is UCP?", "UCP is a commerce protocol surface a merchant can publish for agent discovery and commerce capabilities. A valid declaration is only one part of being ready for AI shopping."),
            ("What does the validator test?", "The current scanner checks profile JSON, version format, declared registries, authority binding, public references, transport liveness and a bounded read-only catalog probe when available."),
            ("What it does not prove", "A public UCP profile does not by itself prove that runtime authorization, replay protection or final-order integrity are enforced."),
        ),
        ("/.well-known/ucp discovery", "Official schema validation when the validator is available", "Namespace and reference authority", "REST, MCP and related transport liveness", "Read-only catalog response quality"),
    ),
    "acp-validator": MarketingPage(
        "/acp-validator",
        "ACP Validator & Security Scanner | Auteric",
        "Detect public Agentic Commerce Protocol declarations and endpoint signals without creating checkout sessions or making payments.",
        "AGENTIC COMMERCE PROTOCOL",
        "ACP validator and security scanner",
        "Auteric is designed to add protocol scanners without reducing readiness to a single standard. The public scanner detects explicit ACP declarations and non-mutating endpoint signals. Checkout lifecycle and payment execution are not tested.",
        (
            ("What works today", "The current scan covers public discovery, catalog quality, UCP, checkout signals and non-destructive web and agentic-commerce security checks."),
            ("Why the page exists now", "Merchants can evaluate the shared foundations needed for agentic commerce alongside conservative ACP public detection."),
            ("No false pass", "Auteric distinguishes detected declarations, missing evidence, blocked probes and untested transaction behavior."),
        ),
        ("Protocol-extensible scan architecture", "Evidence-labelled results", "Catalog and checkout foundations", "Public security posture", "Public ACP detection; execution not tested"),
        "ACP public detection is available; session creation, delegated payment, authority and order completion remain unverified.",
    ),
    "agentic-commerce-security": MarketingPage(
        "/agentic-commerce-security",
        "Agentic Commerce Security | Auteric",
        "Understand and safely assess the security boundaries introduced when AI agents discover products and act on commerce systems.",
        "AGENTIC COMMERCE SECURITY",
        "Make AI commerce useful without exposing the transaction boundary.",
        "Auteric begins with merchant outcomes—visibility, catalog understanding and purchase readiness—then separates public evidence from controls that require verified ownership or a connected runtime.",
        (
            ("Public exposure", "The scanner checks safely observable headers, protocol declarations, trust references and agent-facing endpoints without active exploitation."),
            ("Transaction integrity", "Replay, order mutation, agent identity and policy enforcement require a connected staging or runtime control point before they can be marked tested."),
            ("Responsible disclosure", "Potentially serious details are withheld from anonymous reports. Store ownership must be verified before deeper findings are shown."),
        ),
        ("Public attack-surface signals", "Protocol trust and reference checks", "Prompt-like catalog instruction indicators", "No destructive testing", "Sensitive details withheld until verification"),
    ),
}


def _escape(value: str) -> str:
    return html.escape(value, quote=True)


def render_marketing_page(page: MarketingPage, base_url: str) -> str:
    canonical = f"{base_url.rstrip('/')}{page.path}"
    schema = {
        "@context": "https://schema.org",
        "@graph": [
            {"@type": "Organization", "name": "Auteric Security", "url": base_url.rstrip("/")},
            {
                "@type": "WebApplication",
                "name": "Auteric Agentic Commerce Scanner",
                "applicationCategory": "BusinessApplication",
                "operatingSystem": "Web",
                "url": canonical,
                "description": page.description,
            },
            {
                "@type": "BreadcrumbList",
                "itemListElement": [
                    {"@type": "ListItem", "position": 1, "name": "Auteric", "item": base_url.rstrip("/") + "/"},
                    {"@type": "ListItem", "position": 2, "name": page.heading, "item": canonical},
                ],
            },
        ],
    }
    sections = "".join(
        f'<article><h2>{_escape(title)}</h2><p>{_escape(body)}</p></article>' for title, body in page.sections
    )
    bullets = "".join(f"<li>{_escape(item)}</li>" for item in page.bullets)
    note_id = ' id="protection"' if page.path == "/shopify-ai-shopping" else ""
    note = f'<p class="seo-note"{note_id}>{_escape(page.note)}</p>' if page.note else ""
    contact = ""
    return f'''<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width,initial-scale=1" />
  <meta name="theme-color" content="#173c38" />
  <title>{_escape(page.title)}</title>
  <meta name="description" content="{_escape(page.description)}" />
  <link rel="canonical" href="{_escape(canonical)}" />
  <meta property="og:type" content="website" />
  <meta property="og:site_name" content="Auteric" />
  <meta property="og:title" content="{_escape(page.title)}" />
  <meta property="og:description" content="{_escape(page.description)}" />
  <meta property="og:url" content="{_escape(canonical)}" />
  <meta name="twitter:card" content="summary_large_image" />
  <meta name="twitter:title" content="{_escape(page.title)}" />
  <meta name="twitter:description" content="{_escape(page.description)}" />
  <link rel="icon" type="image/png" href="/static/brand/logo_evergreen_auteric-symbol_20260906_transparent.png" />
  <link rel="apple-touch-icon" href="/static/brand/logo_evergreen_auteric-symbol_20260906_transparent.png" />
  <script type="application/ld+json">{json.dumps(schema, separators=(',', ':')).replace('</', '<\\/')}</script>
  <link rel="stylesheet" href="/static/styles.css?v=021" />
</head>
<body class="seo-body">
  <a class="skip-link" href="#main-content">Skip to content</a>
  <header class="site-header">
    <a class="brand" href="/" aria-label="Auteric Scanner home"><span class="brand-mark brand-image" aria-hidden="true"><img src="/static/brand/logo_evergreen_auteric-symbol_20260906_transparent.png" alt="" width="42" height="42" /></span><span><strong>Auteric</strong><small>Agentic Commerce Scanner</small></span></a>
    <nav class="marketing-nav" aria-label="Primary navigation"><a href="/ai-shopping-readiness">AI readiness</a><a href="/ucp-validator">UCP validator</a><a href="/agentic-commerce-security">Security</a><a class="nav-cta" href="/#scanner">Scan your store</a></nav>
  </header>
  <main id="main-content" class="seo-page">
    <nav class="breadcrumbs" aria-label="Breadcrumb"><a href="/">Auteric</a><span aria-hidden="true">/</span><span>{_escape(page.heading)}</span></nav>
    <section class="seo-hero">
      <span class="eyebrow">{_escape(page.eyebrow)}</span>
      <h1>{_escape(page.heading)}</h1>
      <p>{_escape(page.intro)}</p>
      <form class="scan-form seo-scan" data-scan-form novalidate>
        <label for="seoStoreUrl">Enter your store URL</label>
        <div class="scan-input"><input id="seoStoreUrl" name="storeUrl" autocomplete="url" inputmode="url" placeholder="https://example.com" required /><button class="btn primary" type="submit">Run Free Scan</button></div>
        <p class="form-status" data-form-status role="status" aria-live="polite">Free public scan. No installation required.</p>
      </form>
    </section>
    <section class="seo-content-grid" aria-label="About this check">{sections}</section>
    <section class="seo-checklist"><div><span class="eyebrow">WHAT AUTERIC COVERS</span><h2>One scan, five readiness layers.</h2><p>Results are based on what the scanner can safely observe. Unable to verify is shown honestly.</p></div><ul>{bullets}</ul></section>
    {note}
    {contact}
    <section class="seo-final-cta"><span class="eyebrow">START WITH PUBLIC EVIDENCE</span><h2>See what an AI shopper can understand about your store.</h2><a class="btn primary" href="/#scanner">Scan Your Store</a></section>
  </main>
  <footer class="marketing-footer"><a class="brand" href="/" aria-label="Auteric"><span class="brand-mark brand-image" aria-hidden="true"><img src="/static/brand/logo_evergreen_auteric-symbol_20260906_transparent.png" alt="" width="52" height="52" loading="lazy" decoding="async" /></span><span><strong>Auteric</strong><small>Agentic Commerce Security</small></span></a><nav aria-label="Footer navigation"><a href="/ai-shopping-readiness">AI readiness</a><a href="/shopify-ai-shopping">Shopify</a><a href="/woocommerce-ai-shopping">WooCommerce</a><a href="/ucp-validator">UCP</a><a href="/acp-validator">ACP</a><a href="/">Scanner</a></nav></footer>
  <script src="/static/marketing.js"></script>
</body>
</html>'''


def sitemap_paths() -> list[str]:
    return ["/"] + [page.path for page in PAGES.values()]
