from __future__ import annotations

import asyncio
import hashlib
import re
import socket
import ssl
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

AI_BOTS = ["GPTBot", "OAI-SearchBot", "ClaudeBot", "Google-Extended", "Applebot-Extended", "CCBot"]


def _header_map(headers: Any) -> dict[str, str]:
    return {str(k).lower(): str(v) for k, v in headers.items()}


def analyze_headers(headers: Any, https: bool) -> dict[str, Any]:
    h = _header_map(headers)
    csp = h.get("content-security-policy", "")
    hsts = h.get("strict-transport-security", "")
    sts_age = 0
    m = re.search(r"max-age\s*=\s*(\d+)", hsts, re.I)
    if m:
        sts_age = int(m.group(1))
    controls = {
        "https": https,
        "hsts": bool(hsts),
        "hsts_strong": bool(hsts and sts_age >= 15552000),
        "csp": bool(csp),
        "csp_no_unsafe_eval": "'unsafe-eval'" not in csp.lower() if csp else False,
        "csp_frame_ancestors": "frame-ancestors" in csp.lower() if csp else False,
        "nosniff": h.get("x-content-type-options", "").lower() == "nosniff",
        "referrer_policy": bool(h.get("referrer-policy")),
        "permissions_policy": bool(h.get("permissions-policy")),
        "frame_protection": bool(h.get("x-frame-options")) or "frame-ancestors" in csp.lower(),
        "coop": bool(h.get("cross-origin-opener-policy")),
        "corp": bool(h.get("cross-origin-resource-policy")),
    }
    return {
        "controls": controls,
        "raw": {k: h.get(k) for k in [
            "strict-transport-security", "content-security-policy", "x-content-type-options", "referrer-policy",
            "permissions-policy", "x-frame-options", "cross-origin-opener-policy", "cross-origin-resource-policy",
            "cross-origin-embedder-policy", "server", "x-powered-by", "cache-control"
        ] if h.get(k)},
        "csp": {"present": bool(csp), "unsafe_inline": "'unsafe-inline'" in csp.lower(), "unsafe_eval": "'unsafe-eval'" in csp.lower(), "frame_ancestors": "frame-ancestors" in csp.lower()},
        "hsts": {"present": bool(hsts), "max_age": sts_age, "include_subdomains": "includesubdomains" in hsts.lower(), "preload": "preload" in hsts.lower()},
        "server_disclosure": {"server": h.get("server"), "x_powered_by": h.get("x-powered-by")},
    }


def analyze_cookies(headers: Any) -> dict[str, Any]:
    # httpx Headers exposes get_list; fall back to a single combined value.
    try:
        cookies = headers.get_list("set-cookie")
    except Exception:
        one = headers.get("set-cookie") if hasattr(headers, "get") else None
        cookies = [one] if one else []
    parsed = []
    sensitive_missing = []
    issues = []
    for raw in cookies[:50]:
        if not raw:
            continue
        name = raw.split("=", 1)[0].strip()
        low = raw.lower()
        record = {
            "name": name,
            "secure": "; secure" in low,
            "http_only": "; httponly" in low,
            "same_site": "lax" if "samesite=lax" in low else "strict" if "samesite=strict" in low else "none" if "samesite=none" in low else None,
        }
        parsed.append(record)
        strict_sensitive = bool(re.search(r"session|auth|token|checkout|customer", name, re.I))
        cart_like = bool(re.search(r"cart|basket", name, re.I))
        reasons = []
        if strict_sensitive:
            if not record["secure"]: reasons.append("missing Secure")
            if not record["http_only"]: reasons.append("missing HttpOnly")
        elif cart_like:
            # Cart state is often intentionally readable by storefront JavaScript, so
            # absence of HttpOnly alone is not treated as a security failure.
            if not record["secure"]: reasons.append("missing Secure")
        if reasons:
            issues.append({"name": name, "reasons": reasons, **record, "classification": "sensitive" if strict_sensitive else "cart_state"})
            sensitive_missing.append(name)
    return {"count": len(parsed), "cookies": parsed, "sensitive_without_secure_httponly": sorted(set(sensitive_missing)), "sensitive_cookie_issues": issues}


def analyze_page(html: str) -> dict[str, Any]:
    soup = BeautifulSoup(html, "html.parser")
    og = bool(soup.find("meta", attrs={"property": "og:title"})) and bool(soup.find("meta", attrs={"property": re.compile(r"og:(url|type)", re.I)}))
    viewport = bool(soup.find("meta", attrs={"name": re.compile(r"^viewport$", re.I)}))
    org = False
    for script in soup.find_all("script", attrs={"type": re.compile(r"application/ld\+json", re.I)}):
        txt = (script.string or script.get_text(" ", strip=True) or "").lower()
        if '"organization"' in txt or '"onlinestore"' in txt or '"store"' in txt:
            org = True
            break
    mixed = []
    for tag in soup.find_all(["script", "img", "link", "iframe"]):
        attr = "href" if tag.name == "link" else "src"
        value = tag.get(attr)
        if isinstance(value, str) and value.startswith("http://"):
            mixed.append(value[:180])
            if len(mixed) >= 10:
                break
    return {"open_graph": og, "mobile_viewport": viewport, "organization_jsonld": org, "mixed_content_urls": mixed}


def analyze_robots(text: str) -> dict[str, Any]:
    lines = []
    for raw in text.splitlines():
        clean = raw.split("#", 1)[0].strip()
        if clean:
            lines.append(clean)
    groups: dict[str, list[str]] = {}
    current: list[str] = []
    for line in lines:
        if ":" not in line:
            continue
        key, value = [x.strip() for x in line.split(":", 1)]
        if key.lower() == "user-agent":
            current = [value]
            groups.setdefault(value.lower(), [])
        elif key.lower() == "disallow":
            for agent in current:
                groups.setdefault(agent.lower(), []).append(value)
    bot_access = {}
    for bot in AI_BOTS:
        rules = groups.get(bot.lower(), groups.get("*", []))
        blocked_all = "/" in [r.strip() for r in rules]
        ucp_blocked = blocked_all or any(r and "/.well-known/ucp".startswith(r) for r in rules)
        bot_access[bot] = {"allowed": not blocked_all, "ucp_allowed": not ucp_blocked}
    return {"bot_access": bot_access, "blocks_all": bool(groups.get("*") and "/" in groups.get("*", []))}


async def probe_standard_files(client: Any, root: str, safe_get: Callable[..., Awaitable[Any]]) -> dict[str, Any]:
    results: dict[str, Any] = {}
    for name, path, accept, capture in [
        ("llms_txt", "/llms.txt", "text/plain,*/*", True),
        ("agents_md", "/agents.md", "text/markdown,text/plain,*/*", True),
        ("sitemap", "/sitemap.xml", "application/xml,text/xml,*/*", False),
        ("security_txt", "/.well-known/security.txt", "text/plain,*/*", True),
    ]:
        try:
            r = await safe_get(client, urljoin(root, path), accept=accept)
            text = r.text if r.status_code == 200 else ""
            item = {
                "status": r.status_code,
                "present": r.status_code == 200 and bool(text.strip()),
                "url": str(r.url),
                "size_bytes": len(text.encode("utf-8", errors="ignore")) if text else 0,
            }
            if text:
                item["sha256"] = hashlib.sha256(text.encode("utf-8", errors="ignore")).hexdigest()[:16]
            if capture and text:
                item["content"] = text[:12_000]
                item["truncated"] = len(text) > 12_000
            results[name] = item
        except Exception as exc:
            results[name] = {"status": None, "present": False, "error": str(exc)[:160], "url": urljoin(root, path), "size_bytes": 0}
    return results


async def probe_cors(client: Any, url: str, safe_request: Callable[..., Awaitable[Any]]) -> dict[str, Any]:
    try:
        r = await safe_request(
            client, "OPTIONS", url, accept="*/*",
            headers={"Origin": "https://attacker.invalid", "Access-Control-Request-Method": "POST", "Access-Control-Request-Headers": "content-type,authorization"},
        )
        h = _header_map(r.headers)
        origin = h.get("access-control-allow-origin")
        credentials = h.get("access-control-allow-credentials", "").lower() == "true"
        return {
            "status": r.status_code,
            "allow_origin": origin,
            "allow_credentials": credentials,
            "wildcard_credentials": origin == "*" and credentials,
            "reflects_untrusted_origin": origin == "https://attacker.invalid",
        }
    except Exception as exc:
        return {"status": None, "error": str(exc)[:160], "allow_origin": None, "allow_credentials": False, "wildcard_credentials": False, "reflects_untrusted_origin": False}


async def probe_http_redirect(client: Any, effective_url: str, safe_request: Callable[..., Awaitable[Any]]) -> dict[str, Any]:
    p = urlparse(effective_url)
    if p.scheme != "https":
        return {"tested": False, "redirects_to_https": False}
    http_url = p._replace(scheme="http").geturl()
    try:
        r = await safe_request(client, "GET", http_url, accept="text/html,*/*", follow_redirects=False)
        loc = r.headers.get("location", "")
        resolved = urljoin(http_url, loc) if loc else ""
        return {"tested": True, "status": r.status_code, "location": loc, "redirects_to_https": r.status_code in {301,302,303,307,308} and resolved.lower().startswith("https://")}
    except Exception as exc:
        return {"tested": True, "status": None, "redirects_to_https": False, "error": str(exc)[:160]}


async def probe_tls(effective_url: str) -> dict[str, Any]:
    p = urlparse(effective_url)
    if p.scheme != "https" or not p.hostname:
        return {"tested": False}
    host, port = p.hostname, p.port or 443

    def _do() -> dict[str, Any]:
        ctx = ssl.create_default_context()
        with socket.create_connection((host, port), timeout=5) as raw:
            with ctx.wrap_socket(raw, server_hostname=host) as ssock:
                cert = ssock.getpeercert()
                not_after = cert.get("notAfter")
                expires_at = None
                days = None
                if not_after:
                    expires_at = datetime.strptime(not_after, "%b %d %H:%M:%S %Y %Z").replace(tzinfo=timezone.utc)
                    days = (expires_at - datetime.now(timezone.utc)).days
                return {"tested": True, "version": ssock.version(), "cipher": (ssock.cipher() or [None])[0], "expires_at": expires_at.isoformat() if expires_at else None, "days_to_expiry": days, "certificate_valid": days is None or days >= 0}
    try:
        return await asyncio.get_running_loop().run_in_executor(None, _do)
    except Exception as exc:
        return {"tested": True, "error": str(exc)[:180], "certificate_valid": False}
