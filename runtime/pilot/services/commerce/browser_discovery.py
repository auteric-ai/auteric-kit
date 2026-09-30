"""Operator-only bounded browser observations, not a public SaaS scan endpoint.

HTTP resources are fetched by a DNS-pinned TLS client and fulfilled into a fresh
browser. No browser cookies/authorization, POSTs, WebSockets or service workers.
Production use additionally requires OS/container egress isolation and resource limits.
"""

import argparse
import ipaddress
import json
import re
import socket
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import unquote, urlsplit, urlunsplit

import httpx
from auteric_edge.discovery import catalog_response_candidate, extract_candidates, public_url


def checked_target(url):
    public_url(url)
    parsed = urlsplit(url)
    if re.search(
        r"add[-_]?to[-_]?cart|/cart/(add|change|update|clear)|logout|delete|remove|purchase|place[-_]?order|access_token|api_key|password|secret|token=",
        unquote(url),
        re.I,
    ):
        raise ValueError("Mutation or sensitive URL refused")
    rows = socket.getaddrinfo(parsed.hostname, 443, type=socket.SOCK_STREAM)
    ips = sorted({row[4][0] for row in rows})
    if not ips or any(not ipaddress.ip_address(ip).is_global for ip in ips):
        raise ValueError("All DNS addresses must be public")
    ip = ips[0]
    authority = f"[{ip}]" if ":" in ip else ip
    return urlunsplit(("https", authority, parsed.path or "/", parsed.query, "")), parsed.hostname


def fetch_resource(client, url):
    target, hostname = checked_target(url)
    # The shared fetch client must not replay Set-Cookie, including across virtual
    # hosts sharing one pinned IP. Public observation is always anonymous.
    client.cookies.clear()
    with client.stream(
        "GET",
        target,
        headers={"Host": hostname, "User-Agent": "Auteric-ReadOnly-Discovery/0.1"},
        extensions={"sni_hostname": hostname.encode()},
    ) as response:
        body = bytearray()
        for chunk in response.iter_bytes():
            body.extend(chunk)
            if len(body) > 6_000_000:
                raise ValueError("Resource too large")
        headers = {
            key: value
            for key, value in response.headers.items()
            if key.lower() in {"content-type", "location", "access-control-allow-origin"}
        }
        return response.status_code, headers, bytes(body)


def scan(url, output, max_pages=3):
    from playwright.sync_api import sync_playwright

    source = public_url(url)
    checked_target(source)
    if max_pages not in range(1, 6):
        raise ValueError("Choose 1–5 pages")
    folder = Path(output)
    folder.mkdir(parents=True, exist_ok=False)
    report = {
        "schema_version": 1,
        "url": source,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "status": "running",
        "mode": "public_read_only_observation",
        "pages": [],
        "blocked_requests": 0,
        "observed_get_endpoints": [],
        "api_candidates": [],
        "production_ready": False,
    }
    start = time.monotonic()
    requests = 0
    total_bytes = 0
    with httpx.Client(timeout=8, follow_redirects=False, trust_env=False) as client, sync_playwright() as pw:
        browser = pw.chromium.launch()
        context = browser.new_context(
            service_workers="block", accept_downloads=False, viewport={"width": 1280, "height": 900}
        )
        context.route_web_socket("**/*", lambda ws: ws.close())

        def route_request(route):
            nonlocal requests, total_bytes
            requests += 1
            request = route.request
            try:
                if (
                    request.method != "GET"
                    or requests > 250
                    or total_bytes > 30_000_000
                    or time.monotonic() - start > 70
                ):
                    raise ValueError("Read-only resource budget exceeded")
                if request.is_navigation_request() and urlsplit(request.url).netloc != urlsplit(source).netloc:
                    raise ValueError("Cross-origin document navigation refused")
                code, headers, body = fetch_resource(client, request.url)
                total_bytes += len(body)
                if request.resource_type in {"xhr", "fetch"} and 200 <= code < 300:
                    endpoint = public_url(request.url)
                    if endpoint not in report["observed_get_endpoints"]:
                        report["observed_get_endpoints"].append(endpoint)
                    candidate = catalog_response_candidate(source, request.url, body)
                    if candidate and not any(c["id"] == candidate["id"] for c in report["api_candidates"]):
                        report["api_candidates"].append(candidate)
                route.fulfill(status=code, headers=headers, body=body)
            except Exception:
                report["blocked_requests"] += 1
                route.abort()

        context.route("**/*", route_request)
        page = context.new_page()
        queue = [source]
        visited = set()
        try:
            while queue and len(visited) < max_pages and time.monotonic() - start < 70:
                target = queue.pop(0)
                if target in visited:
                    continue
                visited.add(target)
                entry = {"requested_url": target}
                try:
                    response = page.goto(target, wait_until="domcontentloaded", timeout=20000)
                    page.wait_for_timeout(1200)
                    entry.update(extract_candidates(page.url, page.content()))
                    entry["candidates"] = (report["api_candidates"] + entry["candidates"])[:100]
                    entry["evidence_type"] = "rendered_browser_html"
                    entry["http_status"] = response.status if response else None
                    entry["status"] = "observed" if response and response.ok else "http_error"
                    entry["title"] = page.title()[:200]
                    screenshot = f"page-{len(report['pages']) + 1}.png"
                    page.screenshot(path=str(folder / screenshot), full_page=False, timeout=5000)
                    entry["screenshot"] = screenshot
                    for candidate in entry["candidates"]:
                        if (
                            candidate["classification"] == "read_navigation"
                            and candidate["target_url"] not in visited
                            and urlsplit(candidate["target_url"]).netloc == urlsplit(source).netloc
                        ):
                            queue.append(candidate["target_url"])
                except Exception as error:
                    entry["status"] = "could_not_observe"
                    entry["error_type"] = type(error).__name__
                report["pages"].append(entry)
        finally:
            context.close()
            browser.close()
    report["status"] = (
        "observed" if report["pages"] and all(p["status"] == "observed" for p in report["pages"]) else "partial"
    )
    report["finished_at"] = datetime.now(timezone.utc).isoformat()
    report["limits"] = {
        "max_pages": max_pages,
        "requests": requests,
        "bytes": total_bytes,
        "full_catalog": False,
        "mutation_tests": "not_run",
        "protocol_tests": "not_run",
    }
    (folder / "report.json").write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url")
    parser.add_argument("--output", required=True, help="New local evidence directory")
    parser.add_argument("--max-pages", type=int, default=3)
    args = parser.parse_args()
    report = scan(args.url, args.output, args.max_pages)
    print(
        json.dumps(
            {
                "status": report["status"],
                "pages": len(report["pages"]),
                "candidates": sum(len(p.get("candidates", [])) for p in report["pages"]),
                "output": args.output,
            }
        )
    )


if __name__ == "__main__":
    main()
