from __future__ import annotations

import json
import math
import os
import re
from collections import Counter
from urllib.parse import urlencode, urlsplit

from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from marketing import PAGES, render_marketing_page, sitemap_paths
from public_pages import (CRAWLERS, NAV, PLATFORMS, aggregates, data_paths, editorial_pages,
                          esc, link, render_page, stores_table, table)
from public_reports import domain_name, report_is_readable
from onboarding_kit import kit_context
from merchant_contact import install_contact_routes
from adapters.generic_web import _is_loopback_host, _loopback_targets_allowed

AGENT_ICONS = {
    'codex': '<svg viewBox="0 0 24 24" aria-hidden="true"><rect x="2" y="2" width="20" height="20" rx="5" fill="#7657ff"/><path d="M9 7 5 12l4 5m6-10 4 5-4 5" fill="none" stroke="#fff" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg>',
    'claude': '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 2v20M2 12h20M5 5l14 14M19 5 5 19" stroke="#c76d4f" stroke-width="3" stroke-linecap="round"/></svg>',
    'cursor': '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 3 19 12l-7 1-3 7z" fill="#14121a" stroke="#14121a" stroke-linejoin="round"/></svg>',
    'any': '<svg viewBox="0 0 24 24" aria-hidden="true"><rect x="2" y="4" width="20" height="16" rx="3" fill="none" stroke="#14121a" stroke-width="2"/><path d="m6 9 3 3-3 3m6 0h6" fill="none" stroke="#14121a" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg>',
}


def install_public_routes(app, reports, store, shell, base_url, claims=None, runtime=None):
    pages = editorial_pages()
    install_contact_routes(app)

    def merchant_row(row):
        status = claims.status(row['domain']) if claims else 'unclaimed'
        if runtime:
            status = runtime.status(row['domain'], status != 'unclaimed')
            row = runtime.apply(row)
        return {
            **row,
            'merchant_status': status,
            'protection_score': 100 if status == 'protected' else None,
            'protection_status': 'verified' if status == 'protected' else 'safe_test_required',
        }

    def rows():
        return [merchant_row(r) for r in reports.rows()]

    def latest(domain):
        try:
            normalized = domain_name(domain)
        except (ValueError, UnicodeError):
            raise HTTPException(404, 'Public report not found') from None
        found = reports.rows(normalized, history=True)
        if not found:
            raise HTTPException(404, 'No qualifying public report for this domain')
        current = next((row for row in found if report_is_readable(row)), found[0])
        return [current, *(row for row in found if row['scan_id'] != current['scan_id'])]

    def response(request, path, title, intro, body, index=True):
        indexed = index and not request.query_params
        return HTMLResponse(render_page(path, title, intro, body, base_url(request), index=indexed),
                            headers={'X-Robots-Tag': 'index, follow' if indexed else 'noindex, follow'})

    def merchant_shell(request, path, title, description, body):
        """Use the Scanner's familiar shell for merchant conversion steps."""
        rendered = shell(request, title=title, description=description, path=path, noindex=True)
        markup = rendered.body.decode()
        markup = markup.replace('<body>', '<body class="merchant-flow">', 1)
        markup = markup.replace('<main id="main-content">', '<main id="main-content">' + body)
        markup = markup.replace('class="landing" id="landingView"', 'class="landing hidden" id="landingView"')
        return HTMLResponse(markup, headers={'X-Robots-Tag': 'noindex, follow'})

    @app.get('/store/{domain}')
    @app.get('/scan/{domain}')
    def report(domain: str, request: Request):
        # Local scans are deliberately not eligible for public reports, claims,
        # indexing or onboarding. They still need an ID-bound result page.
        requested = request.query_params.get('id') or request.query_params.get('scan')
        if requested:
            meta = store.metadata(requested)
            result = store.get(requested) if meta and meta['adapter'] == 'generic' else None
            target_host = (urlsplit(str(result.get('target_url') or '')).hostname or '').lower().rstrip('.') if result else ''
            if target_host == domain.lower().rstrip('.') and _is_loopback_host(target_host):
                rendered = shell(request, title=f'{target_host} Local Scan | Auteric', path='/store/' + target_host, noindex=True)
                markup = rendered.body.decode().replace(
                    '<body>',
                    '<body data-scan-id="' + esc(requested) + '" data-store-domain="' + esc(target_host) + '" data-auteric-protected="false" data-local-scan="true">',
                    1,
                )
                return HTMLResponse(markup, headers={'X-Robots-Tag': 'noindex, nofollow'})
        try:
            normalized = domain_name(domain)
        except (ValueError, UnicodeError):
            raise HTTPException(404, 'Invalid public domain') from None
        # Preserve in-flight and legacy ID links without publishing a mismatched record.
        if requested:
            meta = store.metadata(requested)
            if meta and meta['adapter'] == 'generic':
                result = store.get(requested)
                if result and result.get('status') == 'completed':
                    target = domain_name(result['target_url'])
                    if reports.rows(target):
                        return RedirectResponse('/store/' + target, status_code=303)
                return shell(request, title=f'{normalized} Scan in Progress | Auteric', path='/scan/' + normalized, noindex=True)
        if domain != normalized:
            return RedirectResponse('/store/' + normalized, status_code=308)
        if request.url.path.startswith('/scan/'):
            return RedirectResponse('/store/' + normalized, status_code=308)
        history = latest(normalized)
        row = merchant_row(history[0])
        source_scan_id = row.get('source_scan_id') or row['scan_id']
        rendered = shell(request, title=f"{row.get('store_name') or normalized} AI Shopping Readiness | Auteric",
            description=f"See whether {row.get('store_name') or normalized} is discoverable to AI shopping agents, what commerce actions agents can perform and how protected the store is.",
            path='/store/' + normalized, noindex=bool(request.query_params))
        markup = rendered.body.decode()
        markup = markup.replace('<body>', '<body data-scan-id="' + esc(source_scan_id) + '" data-store-domain="' + esc(normalized) + '" data-auteric-protected="' + ('true' if row.get('merchant_status') == 'protected' else 'false') + '">')
        schema = {'@context': 'https://schema.org', '@type': 'BreadcrumbList', 'itemListElement': [
            {'@type': 'ListItem', 'position': 1, 'name': 'Directory', 'item': base_url(request) + '/directory'},
            {'@type': 'ListItem', 'position': 2, 'name': normalized, 'item': base_url(request) + '/store/' + normalized}]}
        markup = markup.replace('</head>', '<script type="application/ld+json">' + json.dumps(schema).replace('<', '\\u003c') + '</script></head>')
        return HTMLResponse(markup)

    @app.get('/reports/{domain}.json')
    def export(domain: str):
        row = merchant_row(latest(domain)[0])
        return JSONResponse(row, headers={'X-Robots-Tag': 'noindex', 'Content-Disposition': f'attachment; filename="{row["domain"]}-auteric.json"'})

    @app.get('/badge/{domain}.svg')
    def badge(domain: str):
        row = latest(domain)[0]
        exposure_verified = (row.get('auteric') or {}).get('status') == 'verified'
        label = 'Exposure Verified' if exposure_verified else 'Publicly Scanned'
        title = 'trusted signed agent-commerce exposure' if exposure_verified else 'publicly scanned, not certified'
        svg = f'''<svg xmlns="http://www.w3.org/2000/svg" width="300" height="60" role="img" aria-label="Auteric {label} {esc(row['score'])}/100"><title>{esc(row['domain'])}: {title}</title><rect width="300" height="60" rx="8" fill="#173c38"/><text x="14" y="24" fill="white" font-family="sans-serif" font-size="14">Auteric · {label}</text><text x="14" y="46" fill="white" font-family="sans-serif" font-size="13">{esc(row['score'])}/100 · {esc(row['domain'][:30])}</text></svg>'''
        return Response(svg, media_type='image/svg+xml', headers={'Cache-Control': 'public, max-age=300', 'X-Robots-Tag': 'noindex'})

    @app.get('/directory')
    def directory(request: Request, page: int = 1, q: str = '', platform: str = '', protocol: str = '', sort: str = 'newest'):
        all_rows = rows()
        selected = [r for r in all_rows if (q.lower() in r['domain'] or q.lower() in (r.get('store_name') or '').lower()) and (not platform or r['platform'] == platform)
                    and (not protocol or r['protocols'].get(protocol) == 'Detected')]
        if sort in {'discoverable', 'functional', 'protected'}:
            selected.sort(key=lambda r: (r.get('readiness') or {}).get(sort) if (r.get('readiness') or {}).get(sort) is not None else -1, reverse=True)
        if sort in {'score', 'ai', 'catalog', 'security', 'lowest'}:
            selected.sort(key=lambda r: r['score' if sort == 'lowest' else sort] if r['score' if sort == 'lowest' else sort] is not None else -1, reverse=sort != 'lowest')
        if page < 1 or (page > 1 and (page - 1) * 25 >= len(selected)):
            raise HTTPException(404, 'Directory page not found')
        shown = selected[(page - 1) * 25:page * 25]
        form = '<form method="get" action="/directory"><label>Search any store <input placeholder="Domain or store name" name="q" value="' + esc(q) + '" /></label><label>Platform <select name="platform"><option value="">All</option>' + ''.join(f'<option value="{p}" {"selected" if p == platform else ""}>{p}</option>' for p in PLATFORMS) + '</select></label><label>Protocol <select name="protocol"><option value="">All</option>' + ''.join(f'<option value="{p}" {"selected" if p == protocol else ""}>{p.upper()}</option>' for p in ('ucp', 'acp', 'mcp')) + '</select></label><label>Sort <select name="sort">' + ''.join(f'<option value="{p}" {"selected" if p == sort else ""}>{p.title()}</option>' for p in ('newest', 'discoverable', 'functional', 'protected')) + '</select></label><button type="submit">Apply filters</button></form>'
        nav = ''
        for num, label in [(page - 1, 'Previous'), (page + 1, 'Next')]:
            if 1 <= num <= math.ceil(len(selected) / 25):
                nav += link(str(request.url.include_query_params(page=num).replace(scheme='', netloc='')), label) + ' '
        body = f'{form}<p>{len(selected)} matching domains; showing {len(shown)}. Page {page}.</p>' + stores_table(shown) + nav
        if not selected and q:
            try:
                candidate = domain_name(q)
                body += '<p>' + link('/?domain=' + candidate, 'Scan this store: ' + candidate) + '</p>'
            except ValueError:
                body += '<p>Enter a domain to scan a store that is not listed yet.</p>'
        body += '<h2>Explore platforms and protocols</h2>' + ' · '.join(link(p, p.split('/')[-1]) for p in data_paths(all_rows) if p.startswith(('/platform/', '/protocol/', '/crawler/')))
        return response(request, '/directory', 'Public Agentic Commerce Directory', 'Browse real public store observations, with crawlable report links and bounded pagination.', body)

    @app.get('/stats')
    @app.get('/leaderboard')
    @app.get('/leaderboard/{category}')
    @app.get('/platform/{category}')
    @app.get('/protocol/{category}')
    @app.get('/crawler/{category}')
    @app.get('/research/agentic-commerce-index')
    def data_page(request: Request, category: str = ''):
        path, selected = request.url.path, rows()
        extra = ''
        title = 'Agentic Commerce Scan Statistics'
        if path.startswith('/platform/'):
            if category not in PLATFORMS:
                raise HTTPException(404, 'Unknown platform')
            selected = [r for r in selected if r['platform'] == category]
            title = category.title() + ' AI Shopping Readiness'
            extra = '<h2>Platform guidance</h2><p>Validate the published storefront catalog, canonical product URLs, prices, currency and availability. Platform detection does not imply every store has enabled the same protocols, checkout integrations or crawler rules.</p>'
        elif path.startswith('/protocol/'):
            if category not in {'ucp', 'acp', 'mcp'}:
                raise HTTPException(404, 'Protocol has no measured dataset')
            selected = [r for r in selected if r['protocols'][category] == 'Detected']
            title = category.upper() + ' Public Commerce Exposure'
            extra = '<p>Only detected public evidence is included here; this is not proof of a completed transaction. ' + link('/docs/' + category, 'Detection scope and limitations') + '</p>'
        elif path.startswith('/crawler/'):
            if category not in CRAWLERS:
                raise HTTPException(404, 'Crawler not measured')
            bot = CRAWLERS[category]
            selected = [r for r in selected if bot in r['bots']]
            allowed = sum(r['bots'][bot] for r in selected)
            title = bot + ' Storefront Crawler Rules'
            extra = f'<p>{allowed} allow; {len(selected) - allowed} block in observed robots rules. This does not prove successful crawling, shopping access or inclusion in an AI answer. Review crawler-specific policy before changing robots.txt.</p>'
        elif path.startswith('/leaderboard'):
            title = 'Agent-Ready Store Leaderboard'
            if category in PLATFORMS:
                selected = [r for r in selected if r['platform'] == category]
            elif category == 'ucp':
                selected = [r for r in selected if r['protocols']['ucp'] == 'Detected']
            elif category not in {'', 'ai-shopping', 'catalog', 'security'}:
                raise HTTPException(404, 'Unknown leaderboard')
            key = {'ai-shopping': 'ai', 'catalog': 'catalog', 'security': 'security'}.get(category, 'score')
            selected.sort(key=lambda r: r[key] if r[key] is not None else -1, reverse=True)
            title += ': ' + (category.replace('-', ' ').title() if category else 'Auteric Score')
            extra = ' · '.join(link('/leaderboard/' + slug, slug) for slug in ('ai-shopping', 'catalog', 'security', 'shopify', 'woocommerce', 'ucp'))
        elif path.startswith('/research/'):
            title = 'Auteric Agentic Commerce Index'
            extra = '<p>Live retained observations, not a scheduled publication. Weekly and monthly archives will appear only after dated snapshots are available. No historical trend is inferred from the current sample.</p>'
        distributions = Counter(r['platform'] for r in selected)
        body = aggregates(selected) + extra + '<h2>Platform distribution</h2>' + table([[esc(k), str(v)] for k, v in distributions.items()])
        body += f'<h2>Observed stores</h2><p>Showing up to 25 of {len(selected)} domains. ' + link('/directory', 'Browse the complete directory') + '</p>' + stores_table(selected[:25])
        return response(request, path, title, 'Measured public evidence from retained Auteric scans, with explicit scope and uncertainty.', body, len(selected) >= 3)

    @app.get('/changes')
    def changes(request: Request, platform: str = '', protocol: str = ''):
        grouped = {}
        for row in reports.rows(history=True):
            grouped.setdefault(row['domain'], []).append(row)
        events = []
        for domain, history in grouped.items():
            for current, previous in zip(history, history[1:]):
                if platform and current['platform'] != platform or current['score_version'] != previous['score_version']:
                    continue
                details = []
                if not protocol and current['score'] != previous['score']:
                    details.append(f"Auteric score {previous['score']} → {current['score']}")
                for p in ('ucp', 'acp', 'mcp'):
                    if (not protocol or protocol == p) and current['protocols'][p] != previous['protocols'][p]:
                        details.append(f"{p.upper()}: {previous['protocols'][p]} → {current['protocols'][p]}")
                if not protocol:
                    for bot in current['bots'].keys() & previous['bots'].keys():
                        if current['bots'][bot] != previous['bots'][bot]:
                            details.append(f"{bot} robots rule changed to {'allow' if current['bots'][bot] else 'block'}")
                if details:
                    events.append((current['completed_at'], domain, '; '.join(details)))
        events.sort(reverse=True)
        body = '<p>Changes between consecutive retained qualifying scans. Showing latest 100 events.</p>' + table([[esc(t), link('/store/' + d, d), esc(v)] for t, d, v in events[:100]])
        return response(request, '/changes', 'Public Commerce Scan Changes', 'Observed protocol, score and crawler-rule changes; missing history is not a change event.', body, bool(events))

    @app.get('/compare')
    @app.get('/compare/{pair}')
    def compare(request: Request, pair: str = '', left: str = '', right: str = ''):
        form = '<form action="/compare" method="get"><label>First domain <input name="left" required /></label><label>Second domain <input name="right" required /></label><button>Compare stores</button></form>'
        if left and right:
            try:
                domains = sorted({domain_name(left), domain_name(right)})
            except ValueError:
                raise HTTPException(422, 'Two valid domains are required') from None
            if len(domains) != 2:
                raise HTTPException(422, 'Choose two different stores')
            return RedirectResponse('/compare/' + '-vs-'.join(domains), status_code=303)
        if not pair:
            return response(request, '/compare', 'Compare Agentic Commerce Readiness', 'Compare two existing public store reports using the same scoring model.', form)
        candidates = pair.split('-vs-')
        if len(candidates) != 2:
            raise HTTPException(404, 'Comparison not found')
        selected = [latest(d)[0] for d in candidates]
        canonical = '-vs-'.join(sorted(r['domain'] for r in selected))
        if canonical != pair:
            return RedirectResponse('/compare/' + canonical, status_code=308)
        body = stores_table(selected) + table([[p.upper(), esc(selected[0]['protocols'][p]), esc(selected[1]['protocols'][p])] for p in ('ucp', 'acp', 'mcp')])
        return response(request, '/compare/' + pair, candidates[0] + ' vs ' + candidates[1], 'Compare observed scores and protocol states. Different scan dates and sample sizes may affect comparability.', body, False)

    @app.get('/sitemap.xml')
    def sitemap(request: Request):
        count = len(rows())
        paths = ['/sitemaps/pages.xml'] + [f'/sitemaps/scans-{i}.xml' for i in range(1, math.ceil(count / 1000) + 1)]
        xml = ''.join(f'<sitemap><loc>{esc(base_url(request) + p)}</loc></sitemap>' for p in paths)
        return Response('<?xml version="1.0" encoding="UTF-8"?><sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">' + xml + '</sitemapindex>', media_type='application/xml')

    @app.get('/sitemaps/{section}.xml')
    def sitemap_part(section: str, request: Request):
        records = rows()
        if section == 'pages':
            paths = set(sitemap_paths() + list(pages) + data_paths(records))
            if len(records) < 3:
                paths -= {'/stats', '/leaderboard', '/research/agentic-commerce-index'}
            paths.discard('/changes')  # event-dependent; discovered through navigation
            entries = [(p, None) for p in sorted(paths)]
        elif re.fullmatch(r'scans-[1-9][0-9]*', section):
            num = int(section.split('-')[1])
            if num > math.ceil(len(records) / 1000):
                raise HTTPException(404, 'Sitemap not found')
            records.sort(key=lambda r: r['domain'])
            entries = [('/store/' + r['domain'], r['completed_at']) for r in records[(num - 1) * 1000:num * 1000]]
        else:
            raise HTTPException(404, 'Sitemap not found')
        body = ''.join('<url><loc>' + esc(base_url(request) + p) + '</loc>' + (f'<lastmod>{esc(date)}</lastmod>' if date else '') + '</url>' for p, date in entries)
        return Response('<?xml version="1.0" encoding="UTF-8"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">' + body + '</urlset>', media_type='application/xml')

    @app.get('/api/registry')
    def registry_search(q: str = '', page: int = 1):
        selected = [r for r in rows() if q.casefold() in r['domain'].casefold() or q.casefold() in (r.get('store_name') or '').casefold()]
        if page < 1:
            raise HTTPException(422, 'Page must be positive')
        return {'stores': selected[(page-1)*25:page*25], 'total': len(selected), 'page': page, 'page_size': 25}

    @app.get('/api/registry/stats')
    def registry_stats():
        measured = rows()
        return {'scanned_domains': len(measured),
                'domains_with_observed_actions': sum(any(v == 'Observed' for v in (r.get('readiness') or {}).get('actions', {}).values()) for r in measured),
                'protected_domains': sum(r['merchant_status'] == 'protected' for r in measured),
                'scope': 'retained public reports; not customers or endorsements'}

    @app.get('/api/onboarding/{domain}/verification/{scan_id}')
    def connection_verification(domain: str, scan_id: str):
        try:
            normalized = domain_name(domain)
        except (ValueError, UnicodeError):
            raise HTTPException(404, 'Invalid public domain') from None
        meta = store.metadata(scan_id)
        record = store.get(scan_id) if meta and meta.get('adapter') == 'generic' else None
        if not record or record.get('status') != 'completed' or domain_name(record.get('target_url') or '') != normalized:
            raise HTTPException(404, 'No completed public scan for this domain and scan ID')
        observations = record.get('observations') or {}
        ucp = observations.get('ucp_analysis') or {}
        catalog = observations.get('catalog') or {}
        claimed = claims.status(normalized) != 'unclaimed' if claims else False
        runtime_status = runtime.status(normalized, claimed) if runtime else 'unclaimed'
        attestation = ucp.get('auteric_attestation') or {}
        attestation_verified = attestation.get('status') == 'verified'
        store_id = attestation.get('store_id') if attestation_verified else None
        control_url = os.getenv('AUTERIC_CONTROL_URL', 'https://control.auteric.com').rstrip('/')
        runtime_action_url = (
            control_url + '/console?' + urlencode({'store': store_id, 'onboarding': 'connection-test'})
            if isinstance(store_id, str) and store_id else None
        )
        return JSONResponse({
            'scan_id': scan_id, 'domain': normalized, 'completed_at': record.get('completed_at'),
            'ucp_status': observations.get('ucp_status') or 'unverified',
            'catalog_observed': int(record.get('products_scanned') or 0),
            'catalog_blocked': bool(catalog.get('blocked')),
            'transport_verified': any(p.get('reachable') is True for p in ucp.get('transport_probes') or []),
            'attestation_status': attestation.get('status') or 'not_detected',
            'runtime_protected': runtime_status == 'protected',
            'runtime_status': 'protected' if runtime_status == 'protected' else 'safe_test_required' if attestation_verified else 'connection_required',
            'runtime_action_url': runtime_action_url,
            'runtime_action_label': 'Run safe cart check' if runtime_action_url else None,
            'runtime_action_detail': (
                'Creates an isolated test cart. No payment, checkout completion, order or customer cart is used.'
                if runtime_action_url else None
            ),
        }, headers={'Cache-Control': 'no-store'})

    @app.get('/onboarding')
    def onboarding_entry(request: Request, domain: str = ''):
        if domain.strip():
            try:
                raw = domain.strip()
                parsed = urlsplit(raw if '://' in raw else '//' + raw)
                candidate = (parsed.hostname or raw).lower().rstrip('.')
                if _is_loopback_host(candidate):
                    if not _loopback_targets_allowed():
                        raise ValueError('Loopback onboarding is disabled')
                    normalized = candidate
                else:
                    normalized = domain_name(domain)
            except (ValueError, UnicodeError):
                raise HTTPException(422, 'Enter a valid store domain') from None
            return RedirectResponse('/onboarding/' + normalized, status_code=303)
        body = '<link rel="stylesheet" href="/static/onboarding.css?v=12"><section id="publicReport" class="merchant-onboarding"><header class="onboarding-heading"><span class="onboarding-lock" aria-hidden="true">🔒</span><div><p class="eyebrow">AUTERIC CONNECT</p><h1>Connect your store to Auteric.</h1><p>Choose how your store is built. Auteric prepares only reviewed changes and activates only capabilities with verified runtime evidence.</p></div></header><form class="onboarding-entry-form" action="/onboarding" method="get"><label for="onboardingDomain">Your store website</label><div><input id="onboardingDomain" name="domain" type="text" required autocomplete="url" placeholder="yourstore.com"><button class="btn primary" type="submit">Start connection</button></div></form></section>'
        return merchant_shell(request, '/onboarding', 'Prepare your store | Auteric', 'Start a three-step AI shopping setup for your store.', body)

    @app.get('/onboarding/{domain}')
    @app.get('/onboarding/{domain}/connect/{path}')
    def onboarding(domain: str, request: Request, path: str = ''):
        local_domain = domain.lower().rstrip('.')
        if _is_loopback_host(local_domain):
            if not _loopback_targets_allowed():
                raise HTTPException(404, 'Invalid public domain')
            normalized = local_domain
        else:
            try:
                normalized = domain_name(domain)
            except (ValueError, UnicodeError):
                raise HTTPException(404, 'Invalid public domain') from None
        candidates = reports.rows(normalized)
        row = merchant_row(candidates[0]) if candidates else {'domain': normalized, 'platform': 'custom', 'merchant_status': 'unclaimed'}
        if path and path != 'skill':
            return RedirectResponse('/onboarding/' + normalized, status_code=308)
        domain_escaped = esc(normalized)
        kit = kit_context(base_url(request), normalized)
        store_name = esc(str(row.get('store_name') or normalized))
        favicon_url = esc('https://' + normalized + '/favicon.ico')
        body = '<link rel="stylesheet" href="/static/onboarding.css?v=12"><section id="publicReport" class="merchant-onboarding" data-domain="' + domain_escaped + '">'
        body += '<header class="onboarding-heading"><span class="onboarding-site-mark"><img src="' + favicon_url + '" alt="" loading="eager" referrerpolicy="no-referrer" onerror="this.hidden=true;this.nextElementSibling.hidden=false"><span aria-hidden="true" hidden>' + esc(normalized[:1].upper()) + '</span></span><div class="onboarding-heading-copy"><p class="eyebrow">AUTERIC CONNECT · ' + store_name + '</p><h1 aria-label="Connect ' + domain_escaped + ' to Auteric."><span>Connect</span> <span class="onboarding-domain" title="' + domain_escaped + '">' + domain_escaped + '</span> <span>to Auteric.</span></h1><p>Auteric reviews your existing commerce implementation, prepares only reviewable changes, and exposes only capabilities verified through the Auteric runtime.</p></div></header>'
        body += '<section class="connection-choice" aria-label="Choose how you build your store"><button type="button" class="connection-choice-card" data-connection-kind="agent" aria-pressed="true"><strong>Using a coding agent</strong><span>Codex, Claude Code, Cursor or another agent in your repository.</span></button><button type="button" class="connection-choice-card" data-connection-kind="builder" aria-pressed="false"><strong>Using a website builder</strong><span>Lovable, Base44, Replit or another builder backed by GitHub.</span></button></section>'
        body += '<article class="onboarding-kit" id="installStep" data-connection-panel="agent"><div class="onboarding-card-head"><span class="onboarding-step-number">01</span><div><h2>Connect with your coding agent.</h2><p>Install Auteric once in the store repository, then use its short native connection command. The installed Kit supplies the versioned workflow.</p></div></div>'
        body += '<div class="onboarding-agent-tabs" role="tablist" aria-label="Coding agent">' + ''.join('<button type="button" role="tab" id="agent-' + key + '" aria-controls="agentPanel" aria-selected="' + ('true' if key == 'codex' else 'false') + '" tabindex="' + ('0' if key == 'codex' else '-1') + '" data-agent="' + key + '">' + AGENT_ICONS[key] + '<span>' + label + '</span></button>' for key, label in [('codex', 'Codex'), ('claude', 'Claude Code'), ('cursor', 'Cursor'), ('any', 'Other coding agent')]) + '</div>'
        body += '<div id="agentPanel" role="tabpanel" aria-labelledby="agent-codex"><div class="onboarding-command"><code id="skillCommand">' + esc(kit['agents']['codex']['command']) + '</code><button type="button" id="copySkillCommand" aria-label="Copy install command" title="Copy install command"><svg viewBox="0 0 24 24" aria-hidden="true"><rect x="8" y="8" width="12" height="12" rx="2" fill="none" stroke="currentColor" stroke-width="2"/><path d="M16 8V6a2 2 0 0 0-2-2H6a2 2 0 0 0-2 2v8a2 2 0 0 0 2 2h2" fill="none" stroke="currentColor" stroke-width="2"/></svg></button></div><div class="merchant-actions"><button class="btn primary" id="copySkillPrompt" type="button"><svg viewBox="0 0 24 24" aria-hidden="true"><rect x="8" y="8" width="12" height="12" rx="2" fill="none" stroke="currentColor" stroke-width="2"/><path d="M16 8V6a2 2 0 0 0-2-2H6a2 2 0 0 0-2 2v8a2 2 0 0 0 2 2h2" fill="none" stroke="currentColor" stroke-width="2"/></svg>Copy setup instruction</button><a id="openAgent" class="btn ghost" target="_blank" rel="noopener noreferrer" hidden>Open coding agent</a></div><p id="copySkillStatus" role="status"></p></div>'
        body += '<div class="onboarding-connect-command"><p class="onboarding-command-label">Then run Connect from the store repository</p><div class="onboarding-command"><code id="connectCommand">' + esc(kit['agents']['codex']['invocation']) + '</code></div></div>'
        body += '<div class="onboarding-kit-links"><a href="https://developers.google.com/merchant/ucp/guides/overview" target="_blank" rel="noopener noreferrer">UCP documentation</a>'
        if kit.get('repo_url'):
            body += '<a class="onboarding-github-link" href="' + esc(kit['repo_url']) + '" target="_blank" rel="noopener noreferrer"><svg viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><path d="M12 0C5.37 0 0 5.37 0 12c0 5.31 3.435 9.795 8.205 11.385.6.105.825-.255.825-.57 0-.285-.015-1.23-.015-2.235-3.015.555-3.795-.735-4.035-1.41-.135-.345-.72-1.41-1.23-1.695-.42-.225-1.02-.78-.015-.795.945-.015 1.62.87 1.845 1.23 1.08 1.815 2.805 1.305 3.495.99.105-.78.42-1.305.765-1.605-2.67-.3-5.46-1.335-5.46-5.925 0-1.305.465-2.385 1.23-3.225-.12-.3-.54-1.53.12-3.18 0 0 1.005-.315 3.3 1.23.96-.27 1.98-.405 3-.405s2.04.135 3 .405c2.295-1.56 3.3-1.23 3.3-1.23.66 1.65.24 2.88.12 3.18.765.84 1.23 1.905 1.23 3.225 0 4.605-2.805 5.625-5.475 5.925.435.375.81 1.095.81 2.22 0 1.605-.015 2.895-.015 3.3 0 .315.225.69.825.57C20.565 21.795 24 17.31 24 12c0-6.63-5.37-12-12-12z"/></svg>Star us <span id="onboardingGithubStars" hidden></span></a>'
        body += '</div></article>'
        lovable = kit['builders']['lovable']
        body += '<article class="onboarding-kit builder-panel" data-connection-panel="builder" hidden><div class="onboarding-card-head"><span class="onboarding-step-number">01</span><div><h2>Prepare the source that publishes your store.</h2><p>Choose the builder you actually use. The instructions below change to the practical next action for that builder.</p></div></div><div class="onboarding-agent-tabs builder-tabs" role="tablist" aria-label="Website builder">' + ''.join('<button type="button" role="tab" data-builder="' + key + '" aria-selected="' + ('true' if key == 'lovable' else 'false') + '"><span>' + esc(item['label']) + '</span></button>' for key, item in kit['builders'].items()) + '</div><div class="builder-detail" id="builderPanel"><h3 id="builderTitle">' + esc(lovable['title']) + '</h3><p id="builderDescription">' + esc(lovable['description']) + '</p><ol id="builderSteps">' + ''.join('<li>' + esc(step) + '</li>' for step in lovable['steps']) + '</ol><button id="checkBuilderStore" class="btn primary" type="button">Check published store now</button><p id="builderCheckExplanation" class="onboarding-fineprint">This is a read-only public check of the published store, UCP and catalog signals. It does not read your builder project or GitHub repository.</p><a id="githubOnboarding" class="btn primary"' + ((' href="' + esc(kit['github_onboarding_url']) + '"') if kit['github_onboarding_available'] else ' hidden') + '>Connect GitHub</a><p id="githubPending" class="onboarding-fineprint"' + (' hidden' if kit['github_onboarding_available'] else '') + '>GitHub connection is not available in this Scanner yet, so no repository access or pull request can be created from this page. Keep the repository URL ready for the authorized Cloud flow.</p></div></article>'
        body += '<button type="button" class="onboarding-step-arrow" data-next-step="publishStep" aria-label="Go to step 2">↓ <span>Next: publish</span></button>'
        body += '<section class="onboarding-publish" id="publishStep"><span class="onboarding-step-number">02</span><div><h2>Review and publish your store changes.</h2><p>Your team approves the product, cart and checkout changes before publishing to your own website.</p></div></section>'
        body += '<section class="onboarding-contact" aria-labelledby="contactHeading"><div><h2 id="contactHeading">Want us to connect your store?</h2><p>Leave your email and phone number. We’ll contact you about connecting <strong>' + domain_escaped + '</strong>.</p></div><form id="connectionHelpForm"><label>Email<input name="email" type="email" autocomplete="email" required maxlength="254" placeholder="you@store.com"></label><label>Phone<input name="phone" type="tel" autocomplete="tel" required maxlength="32" placeholder="+1 555 123 4567"></label><input name="website" type="text" tabindex="-1" autocomplete="off" aria-hidden="true" class="contact-honeypot"><button type="submit" class="btn primary">Request help connecting</button></form><p id="connectionHelpStatus" role="status" aria-live="polite"></p><a id="connectionHelpEmail" hidden href="mailto:hello@auteric.com">Open email instead</a></section>'
        body += '<button type="button" class="onboarding-step-arrow" data-next-step="verifyStep" aria-label="Go to step 3">↓ <span>Next: verify</span></button>'
        body += '<section class="onboarding-verify" id="verifyStep"><div class="onboarding-verify-heading"><span class="onboarding-step-number">03</span><div><h2>Verify the published connection.</h2><p>Check the live catalog, agent interface and trusted Auteric evidence.</p></div><button type="button" class="btn primary" id="checkConnection">Check my store</button></div>'
        body += '<div class="playground-session" aria-live="polite"><div class="playground-session-head"><div class="session-lights" aria-hidden="true"><i></i><i></i><i></i></div><strong id="sessionDomain">' + domain_escaped + '</strong><span class="session-status" id="sessionStatus">NOT CHECKED</span></div><div class="playground-log" id="playgroundLog">'
        for number, label in enumerate(('Read UCP connection', 'Test declared transports', 'Read public products', 'Verify Auteric exposure certificate', 'Confirm protected cart actions'), 1):
            body += '<div class="playground-log-row"><i>0' + str(number) + '</i><div><strong>' + label + '</strong><small>Run a check after publishing your changes.</small></div><em>WAITING</em></div>'
        body += '</div></div><div class="runtime-next-action" id="runtimeNextAction" hidden><div><strong>One safe check remains</strong><p id="runtimeActionDetail">Creates an isolated test cart. No payment, order or customer cart is used.</p></div><a class="btn primary" id="runtimeAction" href="#">Run safe cart check</a></div><p id="connectionCheckStatus" role="status" aria-live="polite"></p><p class="onboarding-fineprint">The signed exposure is already verified. The safe cart check confirms that allowed actions pass through the Gateway and merchant policy; protection activates automatically when it passes.</p></section>'
        body += '<section class="onboarding-after"><span aria-hidden="true">🛡</span><div><h2>After the safe check</h2><p>Protected Agent Access turns on automatically. Your storefront and checkout stay unchanged, and no second Connect command is required.</p></div></section>'
        body += '<p class="onboarding-fineprint">Copying the instruction does not activate a production connection or protection by itself. Protection requires validated runtime evidence.</p>'
        body += '<a class="onboarding-back" href="/store/' + domain_escaped + '">Back to store result</a>' if candidates else '<a class="onboarding-back" href="/">Back to Scanner</a>'
        body += '<script type="application/json" id="onboardingKitConfig">' + json.dumps(kit).replace('<', '\\u003c') + '</script></section><script src="/static/onboarding.js?v=12" defer></script>'
        return merchant_shell(request, '/onboarding/' + normalized, 'Connect ' + normalized + ' | Auteric', 'Connect your store to Auteric with one coding-agent instruction.', body)

    @app.get('/{section}/{slug}', include_in_schema=False)
    def editorial(section: str, slug: str, request: Request):
        path = '/' + section + '/' + slug
        if path not in pages:
            raise HTTPException(404, 'Page not found')
        markup = render_marketing_page(pages[path], base_url(request))
        markup = markup.replace('<main id="main-content" class="seo-page">', '<main id="main-content" class="seo-page">' + NAV)
        if request.query_params:
            markup = markup.replace('</head>', '<meta name="robots" content="noindex,follow" /></head>')
        return HTMLResponse(markup)

    for path in ('/methodology', '/docs'):
        def page_handler(request: Request):
            return HTMLResponse(render_marketing_page(pages[request.url.path], base_url(request)).replace('<main id="main-content" class="seo-page">', '<main id="main-content" class="seo-page">' + NAV))
        app.add_api_route(path, page_handler, methods=['GET'])
