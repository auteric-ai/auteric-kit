"""Server-rendered public pages, using the existing scanner component classes."""
from __future__ import annotations

import html
import json
from collections import Counter
from dataclasses import replace
from urllib.parse import quote

from marketing import MarketingPage, PAGES, render_marketing_page
from readiness import VERSION as READINESS_VERSION


def esc(value):
    return html.escape(str(value), quote=True)


def link(path, label):
    return f'<a href="{esc(path)}">{esc(label)}</a>'


NAV = '<nav aria-label="Explore scanner data">' + ' · '.join(link(p, t) for p, t in (
    ("/directory", "Directory"), ("/stats", "Statistics"), ("/leaderboard", "Leaderboard"),
    ("/changes", "Changes"), ("/compare", "Compare stores"), ("/methodology", "Methodology"),
    ("/docs", "Documentation"), ("/research/agentic-commerce-index", "Commerce index"))) + '</nav>'


def table(rows):
    body = ''.join('<tr>' + ''.join(f'<td>{cell}</td>' for cell in row) + '</tr>' for row in rows)
    return f'<div class="table-scroll"><table><tbody>{body}</tbody></table></div>'


def stores_table(rows):
    return table([["Store", "Discoverable", "Functional", "Protected", "Status", "Last scanned"]] + [
        [link('/store/' + r['domain'], r.get('store_name') or r['domain']),
         *[esc((r.get('readiness') or {}).get(k) if (r.get('readiness') or {}).get(k) is not None else 'Unverified') for k in ('discoverable', 'functional', 'protected')],
         esc(r.get('merchant_status', 'unclaimed')), esc(r['completed_at'])] for r in rows])


def report_body(row, history, base):
    domain = row['domain']
    r = row.get('readiness')
    if not r:
        return '<section id="publicReport" class="seo-page"><h1>' + esc(domain) + ' AI Shopping Readiness</h1><p>This historical scan predates the three-score model. Rescan to measure current readiness.</p>' + link('/?domain=' + domain, 'Scan this store') + '<details><summary>View Technical Report</summary>' + technical_report_body(row, history, base).replace('id="publicReport"', 'id="technicalLegacy"') + '</details></section>'
    status = row.get('merchant_status', 'unclaimed')
    score_cards = []
    for key, label, desc in [('discoverable', 'Discoverable', 'Can agents understand your catalog?'),
                             ('functional', 'Functional', 'Which shopping actions were observed?'),
                             ('protected', 'Auteric Protection', 'Measured only after a safe, non-payment cart check')]:
        value = r[key] if key != 'protected' or status == 'protected' else None
        css = 'is-good' if value is not None and value >= 70 else 'is-action' if value is not None else 'is-pending'
        shown = str(value) + '<small>/100</small>' if value is not None else '<span class="score-unverified">Safe test needed</span>'
        score_cards.append(f'<article class="merchant-score {css}"><h2>{label}</h2><strong>{shown}</strong><p>{desc}</p></article>')
    cards = ''.join(score_cards)
    current_model = r.get('version') == READINESS_VERSION
    c = r['coverage']
    coverage_text = f"{c['readable']} / {c['sampled']} sampled products have the required information."
    if c['public_total'] is not None:
        coverage_text += f" Analyzed {c['sampled']} of {c['public_total']} products in the observed public source."
    if c['coverage_percent'] is not None:
        coverage_text += f" {c['coverage_percent']}% public-source coverage."
    coverage_text += ' This is sampling confidence, not a score or a finding that the remaining products are defective.'
    history_rows = [x for x in history if (x.get('readiness') or {}).get('version') == r['version']]
    hist = table([["Scan time", "Discoverable", "Functional", "Protected"]] + [
        [esc(x['completed_at']), *[esc(x['readiness'][k] if x['readiness'][k] is not None else 'Unverified') for k in ('discoverable', 'functional', 'protected')]] for x in history_rows[:20]])
    catalog_observation = (row.get('observations') or {}).get('catalog') or {}
    public_catalog_blocked = bool(catalog_observation.get('blocked'))
    claim = link('/claim/' + domain, 'Is this your store? Claim it') if status == 'unclaimed' else '<a href="/onboarding/' + esc(domain) + '" target="_blank" rel="noopener noreferrer">Continue setup</a>'
    summary = ('Your storefront asked us not to read its catalog automatically. We did not bypass that protection. Connect a merchant-authorized feed, API or Auteric Skill to measure catalog readiness.' if public_catalog_blocked else
               'No readable product catalog was observed. This does not mean your catalog is missing; connect a feed, API or Auteric Skill for a complete assessment.' if not c['sampled'] else
               'Here is what Auteric verified, what it could not verify, and the shortest path to improve it.')
    headline = ('Your store needs a permitted catalog connection' if public_catalog_blocked else
                'Your store is partially ready for AI shopping' if c['sampled'] else 'Help AI agents discover your store')
    if status == 'protected':
        headline = 'Your verified agent actions are protected'
    fixes = ''.join('<li>' + esc(x) + '</li>' for x in r['recommendations'][:3])
    setup_note = ''
    if public_catalog_blocked:
        setup_note += '<aside class="merchant-note merchant-note-blocked"><strong>Public scan paused by bot protection</strong><p>Auteric detected storefront protection and did not attempt to bypass it. This is not a store defect and does not lower your score. Use a merchant-authorized connection to inspect the catalog.</p></aside>'
    if r['actions'].get('Search products') == 'Scanner setup required':
        setup_note = '<aside class="merchant-note"><strong>Auteric setup needed</strong><p>We found the store\'s MCP endpoint, but this local Scanner has no public HTTPS agent profile, so it could not safely test catalog search. This is not a merchant finding.</p></aside>'
    technical = technical_evidence_body(row, r)
    return f'''<section id="publicReport" class="seo-page merchant-report" data-public-domain="{esc(domain)}">
      <p class="eyebrow">{esc(row.get('store_name') or domain)} · {esc(domain)} · {esc(status)}</p>
      <h1>{esc(headline)}</h1>
      <p>Get discovered. Get shoppable. Stay protected.</p>
      <div class="merchant-scores">{cards}</div>
      <p>{esc(summary)}</p>
      <h2>What to do next</h2><ol class="merchant-fixes">{fixes}</ol>
      <div class="merchant-actions"><a class="btn primary" href="/onboarding/{esc(domain)}" target="_blank" rel="noopener noreferrer">Prepare safer AI shopping</a>
      <button class="btn ghost" type="button" data-open-catalog>Audit more catalog pages</button></div><p>{claim}</p>
      <h2>How much did we inspect?</h2><p><strong>{esc(r.get('assessment_confidence', 'Earlier scan sample; rescan to calculate confidence with the current model'))}</strong></p><p>{esc(coverage_text)}</p>
      <h2>Agents can currently</h2>{table([[esc(k), esc(v)] for k, v in r['actions'].items()])}
      <p>Observed means a public read succeeded. Declared means advertised only. Scanner setup required means Auteric needs configuration; it is not a merchant gap. Unverified does not mean unsupported.</p>
      {setup_note}
      <h2>Your platform: {esc(row['platform'])}</h2><p>{esc(r['platform_guidance'])}</p>
      <p>Protected by Auteric: {esc('verified by current runtime evidence' if status == 'protected' else r['protection_status'])}. Public exposure signatures do not verify runtime enforcement.</p>
      {'' if current_model else '<aside class="merchant-note"><strong>This report uses an earlier readiness model</strong><p>Run a fresh scan to separate sample confidence from catalog quality and to apply the latest capability checks.</p></aside>'}
      <h2>Measured score history</h2><p>Latest {min(20, len(history_rows))} of {len(history_rows)} observations using {esc(r['version'])}. Improvements are not attributed to Auteric without connected evidence.</p>{hist}
      <p>Last scan: <time>{esc(row.get('source_scan_completed_at') or row['completed_at'])}</time></p>
      <details id="technical-report"><summary>Technical evidence</summary><button class="btn ghost" data-technical-dashboard="{esc(row.get('source_scan_id') or row['scan_id'])}">Open interactive catalog and checks</button><p data-technical-status role="status"></p>{technical}</details>
    </section>'''


def technical_evidence_body(row, readiness):
    """Keep protocol evidence available without repeating legacy scores or CTAs."""
    c = readiness['coverage']
    source = (row.get('observations') or {}).get('catalog') or {}
    protocol_rows = [["UCP", row['protocols']['ucp']], ["ACP", row['protocols']['acp']], ["MCP", row['protocols']['mcp']]]
    catalog_rows = [
        ["Source", source.get('source', 'Unknown').replace('_', ' ')],
        ["Products analyzed", c['sampled']],
        ["Public-source total", c['public_total'] if c['public_total'] is not None else 'Unknown'],
        ["Assessment confidence", readiness.get('assessment_confidence', 'Earlier scan; rescan to calculate')],
    ]
    return f'''<section class="merchant-technical" id="technicalLegacy">
      <h2>Public evidence</h2>
      {table(catalog_rows)}
      <h2>Protocols detected</h2>
      {table(protocol_rows)}
      <p>Technical details do not establish cart execution, payment completion or runtime protection. The interactive view remains available above for product-by-product inspection.</p>
    </section>'''


def render_page(path, title, description, body, base, *, index=True):
    page = MarketingPage(path, title + ' | Auteric', description, 'AUTERIC PUBLIC SCANNER', title,
                         description, (), (), None)
    markup = render_marketing_page(page, base)
    start = markup.index('    <section class="seo-content-grid"')
    end = markup.index('    <section class="seo-final-cta"', start)
    markup = markup[:start] + f'<section class="seo-checklist"><div>{NAV}{body}</div></section>' + markup[end:]
    if not index:
        markup = markup.replace('</head>', '<meta name="robots" content="noindex,follow" /></head>')
    return markup


def technical_report_body(row, history, base):
    domain = row['domain']
    auteric = row.get('auteric') or {}
    auteric_verified = auteric.get('status') == 'verified'
    auteric_summary = (
        'Auteric signed exposure verified for the advertised agent-commerce surface. '
        'This does not mean ordinary storefront traffic is routed through Auteric.'
        if auteric_verified else
        'No trusted Auteric exposure signature was verified in this scan.'
    )
    score = f"{row['score']}/100 · Grade {row['grade']}" if row['score'] is not None else 'Not scored'
    summary = (f"{domain} has {row['products_scanned']} product records in its scanned sample. "
               f"UCP: {row['protocols']['ucp']}; ACP: {row['protocols']['acp']}; MCP: {row['protocols']['mcp']}. "
               "Agent-completable checkout and delegated payment were not verified by this public scan.")
    protection_score = 100 if row.get('merchant_status') == 'protected' else None
    protection_label = f"{protection_score}/100" if protection_score is not None else "Safe test needed"
    score_rows = [["Public AI commerce index", esc(score)], ["AI Shopping Readiness", esc(row['ai'])],
                  ["Catalog Readiness", esc(row['catalog'])], ["Auteric protection", protection_label],
                  ["Public web and agent security checks", esc(row['security'])]]
    facts = table([[esc(k), esc(v)] for k, v in row['capabilities'].items()])
    fields = table([[esc(k.replace('_', ' ').title()), f"{v} observed records"] for k, v in row['product_fields'].items()])
    protocols = table([[k.upper(), esc(v)] for k, v in row['protocols'].items()] + [
        ["ACP evidence source", esc(row['acp_source'] or 'Not detected')],
        ["ACP checkout endpoint signal", esc(row['acp_checkout'])],
        ["ACP discovery/feed, lifecycle, delegated payment, authority, provider, completion", "Not yet tested"],
        ["UCP declared payment handlers", str(row['payment_handlers'])],
        ["UCP version", esc(row['ucp_version'])]])
    bots = table([[esc(k), 'Allowed by observed robots rules' if v else 'Blocked by observed robots rules'] for k, v in row['bots'].items()]) if row['bots'] else '<p>Crawler rules: unable to verify.</p>'
    hist = table([[esc(r['completed_at']), esc(r['score']), esc(r['protocols']['ucp']), esc(r['protocols']['acp']), esc(r['protocols']['mcp'])] for r in history[:20]])
    url = base + '/scan/' + domain
    badge_url = base + '/badge/' + domain + '.svg'
    badge_label = 'Auteric Exposure Verified' if auteric_verified else 'Publicly Scanned by Auteric'
    embed = f'<a href="{url}"><img src="{badge_url}" alt="{domain}: {badge_label}" /></a>'
    md = f'[![{badge_label}]({badge_url})]({url})'
    platform = link('/platform/' + row['platform'], row['platform']) if row['platform'] != 'unknown' else 'Unknown; not assigned to Custom'
    links = ' · '.join(link('/protocol/' + p, p.upper()) for p in ('ucp', 'acp', 'mcp'))
    return f'''<section id="publicReport" class="seo-page" data-public-domain="{esc(domain)}" data-public-scan="{esc(row['scan_id'])}">
    <h1>{esc(domain)} Agentic Commerce Readiness Report</h1>
    <p>{esc(summary)}</p><p>Publicly Scanned · {platform} · Last scan: <time>{esc(row['completed_at'])}</time></p>
    <h2>Public AI commerce index</h2>{table(score_rows)}
    <p>The public index combines AI readiness, catalog and public security checks. Auteric protection is shown as unmeasured until one safe, non-payment cart check produces current runtime evidence. Public security checks do not prove Auteric protection. {link('/docs/scoring', 'Scoring details')}</p>
    <h2>What AI agents can currently do on {esc(domain)}</h2>{facts}
    <h2>Protocols and payment declarations</h2>{protocols}<p>{links}</p>
    <p>MCP coverage is limited to UCP-declared transports. Detection is not transaction verification.</p>
    <h2>Auteric verification</h2><p><strong>{esc(auteric.get('label') or 'Auteric attestation not detected')}</strong></p>
    <p>{esc(auteric_summary)}</p>
    <h2>Catalog observations</h2><p>{row['products_scanned']} analyzed records. {'Catalog pagination completed for the public source.' if row['catalog_complete'] else 'Sample only; full-catalog coverage is not established.'}</p>{fields}
    <h2>AI crawler accessibility</h2><p>robots.txt HTTP status: {esc(row['robots_status'] or 'Unable to verify')}. Rules do not guarantee crawler access or search inclusion.</p>{bots}
    <h2>Agentic Commerce Security</h2><p>Auteric protection: {protection_label}. Public web and agent checks: {esc(row['security'])}/100. Detailed findings, endpoint evidence and runtime controls remain private. Missing evidence is not a vulnerability. {'Signed exposure is verified; one safe cart check is needed to measure runtime enforcement.' if auteric_verified else 'Ownership and protection are not verified.'}</p>
    <p>{link('/security/agentic-commerce', 'Authority, checkout integrity and payment scope')} · {link('/onboarding/' + domain, 'Run the safe protection check' if auteric_verified else 'Connect this store')}</p>
    <h2>Scan history</h2><p>Latest {min(20, len(history))} of {len(history)} retained qualifying public observations. Scores are compared only within the same scoring version.</p>{hist}
    <h2>Share this public report</h2><p>{link('/reports/' + domain + '.json', 'Export public JSON')} · <button type="button" data-public-copy>Copy link</button> · <button type="button" data-public-print>Print report</button> · {link('https://www.linkedin.com/sharing/share-offsite/?url=' + quote(url, safe=''), 'Share on LinkedIn')} · {link('https://twitter.com/intent/tweet?url=' + quote(url, safe=''), 'Share on X')}</p>
    <h2>{'Auteric Exposure Verified' if auteric_verified else 'Publicly Scanned'} badge</h2><p>{'This badge confirms a trusted Auteric signature on the advertised agent-commerce surface; it does not certify all storefront traffic.' if auteric_verified else 'This badge does not claim Verified Merchant or Auteric Protected status.'}</p><img src="{esc(badge_url)}" alt="{'Auteric Exposure Verified' if auteric_verified else 'Publicly Scanned'} badge" width="300" height="60" />
    <details><summary>HTML and Markdown embeds</summary><pre><code>{esc(embed)}</code></pre><pre><code>{esc(md)}</code></pre></details>
    {NAV}</section>'''


TOOLS = {
    'ucp-checker': ('UCP Checker', 'ucp-validator'),
    'ucp-validator': ('UCP Manifest Validator', 'ucp-validator'),
    'acp-checker': ('ACP Public Evidence Checker', 'acp-validator'),
    'mcp-commerce-checker': ('MCP Commerce Exposure Checker', 'ucp-validator'),
    'ai-shopping-readiness': ('AI Shopping Readiness Scanner', 'ai-shopping-readiness'),
    'ai-crawler-checker': ('AI Crawler Accessibility Checker', 'chatgpt-shopping'),
    'catalog-readiness': ('Catalog Readiness Checker', 'ai-shopping-readiness'),
    'agentic-commerce-security-scanner': ('Agentic Commerce Security Scanner', 'agentic-commerce-security'),
}


def editorial_pages():
    pages = {}
    for slug, (title, source) in TOOLS.items():
        base = PAGES[source]
        pages['/tools/' + slug] = replace(base, path='/tools/' + slug, title=title + ' | Auteric', heading=title,
            description=title + ': run the shared Auteric public scanner and open one canonical store report.',
            intro=base.intro + ' This focused entry point runs the same public scanner; it is not a separate product.')
    pages['/tools/mcp-commerce-checker'] = replace(pages['/tools/mcp-commerce-checker'],
        intro='Inspect MCP exposure through UCP-declared transports. Auteric checks transport reachability and attempts a bounded read-only catalog probe. Independent MCP servers without a UCP declaration are not exhaustively discovered.')
    methodology = (
        ('Public scope', 'Bounded public requests inspect storefront HTML, discovery files, catalog records, protocol declarations and safe transport signals. Public scans do not buy products, authenticate as a customer, capture payments or mutate inventory.'),
        ('Evidence and limitations', 'Detected means observed public evidence. Declared means a merchant publishes a capability. Neither proves execution. Unable to verify and Not yet tested do not mean vulnerable or unsupported. Crawler rules cannot guarantee ChatGPT or Gemini inclusion.'),
        ('Public and private evidence', 'Indexed reports use an explicit allowlist of scores, statuses and aggregate counts. Raw headers, cookies, payloads, security finding details, products and connected reports are excluded. Publicly Scanned is not ownership verification or protection.'),
        ('Sampling and quality', 'Index completed generic scans with a recorded storefront response. A store with no observed products still receives guidance; blocked responses remain unverified. Domain/IP restrictions apply. Samples are labelled; unknown platforms are not assumed custom. Submitted stores form a convenience sample, not a representative market survey.'),
        ('History and corrections', 'Retained public observations are immutable and dated. Current pages select the latest qualifying observation per hostname. Contact security@auteric.com for corrections, false positives or disclosure review. A failed rescan does not erase an earlier valid observation.'),
    )
    scoring = (
        ('Merchant scores v3', 'Discoverable weights observed catalog-field quality at 55 points, machine-readable catalog evidence at 15, and identity, public reachability and observed AI-crawler access at 10 each. Sampling changes confidence, never the score. Functional counts observed actions out of 13 defined commerce actions; declarations earn no points. Protected remains unverified without trusted runtime evidence.'),
        ('Auteric public score v1', 'The umbrella score is the rounded equal-weight average of AI Shopping Readiness, Catalog Readiness and Security. AI Shopping Readiness averages the existing discovery, channel and checkout scores. All three inputs must exist; missing inputs are not converted into zero.'),
        ('Catalog and security', 'Catalog uses the existing evidence-weighted product field model, including identity, pricing, availability, variants and structured data. Security uses known public checks for web/TLS and agent trust; unknown transaction controls are excluded. Readiness is not conformance, and security posture is not a certification.'),
        ('Grades and legacy scores', 'A+ >=95; A >=90; B >=80; C >=70; D >=55; F <55. The existing legacy composite remains stored separately, so its historical values are not overwritten. Public score comparisons use public-v1 only.'),
    )
    for path, title, sections in (
        ('/methodology', 'Scanner Methodology', methodology), ('/docs', 'Scanner Documentation', methodology),
        ('/docs/scanner', 'How the Public Scanner Works', methodology[:2]),
        ('/docs/methodology', 'Public Evidence Methodology', methodology[1:]),
        ('/docs/scoring', 'Auteric Score Methodology', scoring),
        ('/docs/security', 'Public Scanner Security Boundaries', methodology[2:]),
    ):
        pages[path] = MarketingPage(path, title + ' | Auteric', title + ': evidence, scope and practical limitations of Auteric public scans.',
            'SCANNER DOCUMENTATION', title, sections[0][1], sections, ('Discoverable', 'Understandable', 'Purchasable', 'Secure'))
    for proto, source in [('ucp', 'ucp-validator'), ('acp', 'acp-validator'), ('mcp', 'ucp-validator')]:
        pages['/docs/' + proto] = replace(PAGES[source], path='/docs/' + proto,
            title=proto.upper() + ' Scanner Coverage | Auteric', heading=proto.upper() + ' Scanner Coverage')
    pages['/docs/mcp'] = replace(pages['/docs/mcp'], intro=pages['/tools/mcp-commerce-checker'].intro)
    topics = {
        'agentic-commerce': ('Agentic Commerce Security', 'Agent identity, delegated authority and transaction scope need connected validation beyond public readiness.'),
        'ucp': ('UCP Security', 'Manifest validity and authority binding do not establish checkout integrity or runtime enforcement.'),
        'acp': ('ACP Security', 'Public ACP markers do not prove session lifecycle, delegated payment or merchant order authority.'),
        'mcp': ('MCP Commerce Security', 'A reachable transport does not establish authorization for each tool or transaction. Public probes stay read-only.'),
        'ai-checkout': ('AI Checkout Security', 'An exposed checkout path is not proof that an agent can safely complete a purchase.'),
        'delegated-payments': ('Delegated Payment Security', 'A payment declaration does not prove consent, amount limits, replay resistance or provider enforcement.'),
        'agent-permissions': ('Commerce Agent Permissions', 'Agent identity, allowed actions, spending scope and expiry must be enforced by a connected runtime.'),
        'commerce-agent-attack-surface': ('Commerce Agent Attack Surface', 'Public signals describe exposure. Exploit details and private transaction evidence are not published.'),
        'ai-commerce-security-checklist': ('AI Commerce Security Checklist', 'Review transport, protocol authority, agent identity, delegated scope, checkout integrity and payment controls.'),
    }
    for slug, (title, intro) in topics.items():
        base = PAGES['agentic-commerce-security']
        path = '/security/' + slug
        pages[path] = replace(base, path=path, title=title + ' | Auteric', heading=title, description=intro, intro=intro,
            sections=((title + ': public evidence', intro), *base.sections))
    return pages


PLATFORMS = ('custom', 'woocommerce', 'wix', 'magento', 'commercetools', 'bigcommerce', 'shopify', 'shopware', 'prestashop')
CRAWLERS = {'gptbot': 'GPTBot', 'oai-searchbot': 'OAI-SearchBot', 'google-extended': 'Google-Extended',
            'googlebot': 'Googlebot', 'claudebot': 'ClaudeBot', 'perplexitybot': 'PerplexityBot'}


def aggregates(rows):
    count = len(rows)
    facts = [('Qualifying public domains', str(count))]
    for key, title in [('score', 'Average Auteric score'), ('ai', 'Average AI readiness'), ('catalog', 'Average catalog'), ('security', 'Average security')]:
        known = [r[key] for r in rows if r[key] is not None]
        facts.append((title, f'{sum(known) / len(known):.1f}/100 ({len(known)} scored domains)' if known else 'Not available'))
    for protocol in ('ucp', 'acp', 'mcp'):
        detected = sum(r['protocols'][protocol] == 'Detected' for r in rows)
        facts.append((protocol.upper() + ' detected', f'{detected}/{count} ({100 * detected / count:.1f}%)' if count else 'No data'))
    return table([[esc(k), esc(v)] for k, v in facts]) + '<p>Denominator: latest qualifying public scan per domain in the retained Auteric dataset. This is a submitted-store sample, not ecosystem-wide adoption. Unknown and untested states are included in the denominator.</p>'


def data_paths(rows):
    paths = ['/directory', '/stats', '/leaderboard', '/changes', '/compare', '/research/agentic-commerce-index']
    paths += ['/platform/' + p for p in PLATFORMS if sum(r['platform'] == p for r in rows) >= 3]
    paths += ['/protocol/' + p for p in ('ucp', 'acp', 'mcp') if sum(r['protocols'][p] == 'Detected' for r in rows) >= 3]
    paths += ['/crawler/' + slug for slug, bot in CRAWLERS.items() if sum(bot in r['bots'] for r in rows) >= 3]
    return paths
