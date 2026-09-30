"""Merchant scores v3: evidence earned, never infer execution from declarations."""
from urllib.parse import urlsplit

VERSION = 'merchant-v3'
ACTIONS = ('Discover products', 'Read product details', 'Read variants', 'Read availability',
           'Search products', 'Create cart', 'Add item', 'Change quantity', 'Remove item',
           'Select variant', 'Get shipping options', 'Checkout handoff', 'Complete payment')
GUIDANCE = {
    'custom': 'Publish a structured catalog or read-only product API. Connect your existing APIs through reviewed Custom mappings and contract tests.',
    'woocommerce': 'Review public product data and variants, then configure a merchant-authorized WooCommerce connector.',
    'wix': 'Review Wix product metadata and connect supported commerce APIs through reviewed mappings.',
    'magento': 'Review Adobe Commerce product and inventory data; map your REST or GraphQL commerce operations.',
    'commercetools': 'Map project product projections, carts and checkout APIs with explicit scopes and contract tests.',
    'bigcommerce': 'Review public catalog fields and map authorized catalog, cart and checkout APIs.',
    'shopify': 'Review product publication and variants, then use the Shopify connector where configured.',
}


def coverage(record):
    products = record.get('products') or []
    # Duplicate records are not additional catalog coverage.
    unique = {}
    for p in products:
        key = p.get('url') or p.get('id')
        if key:
            unique.setdefault(key, p)
    products = list(unique.values())
    fields = ('title', 'description', 'images', 'price', 'currency', 'availability', 'url')
    def present(p, key):
        if key == 'price':
            return isinstance(p.get(key), (float, int)) and p[key] >= 0
        if key == 'url':
            return urlsplit(str(p.get(key) or '')).scheme in {'http', 'https'}
        return bool(p.get(key))
    counts = {k: sum(present(p, k) for p in products) for k in fields}
    def ready(p):
        return all(present(p, k) for k in fields) and all(
            v.get('price') is not None and v.get('available') is not None for v in p.get('variants') or [])
    readable = sum(ready(p) for p in products)
    catalog = (record.get('observations') or {}).get('catalog') or {}
    count = len(products)
    total = catalog.get('total_products') if catalog.get('total_known') is True else None
    if not isinstance(total, int) or isinstance(total, bool) or total < count:
        total = None
    # Pagination completeness describes a public source, never the whole merchant catalog.
    complete = catalog.get('complete') is True and not catalog.get('capped') and not catalog.get('lazy_pagination')
    if complete and total is None:
        total = count
    return {'sampled': count, 'readable': readable, 'field_counts': counts,
            'public_total': total, 'public_source_complete': bool(complete),
            'sample_readable_percent': round(100 * readable / count) if count else None,
            'coverage_percent': round(100 * count / total, 1) if total else None,
            'scope': 'observed public source' if complete else 'sample; merchant catalog total unverified'}


def readiness(record):
    c = coverage(record)
    o = record.get('observations') or {}
    n = c['sampled']
    quality = sum(c['field_counts'].values()) / (7 * n) if n else 0
    catalog_source = str((o.get('catalog') or {}).get('source') or '')
    # A public platform product API is machine-readable catalog evidence even if
    # product-page JSON-LD was not present in the sampled HTML.
    machine_readable = any(p.get('structured_data') for p in record.get('products') or []) or catalog_source in {
        'shopify_public_api', 'woocommerce_store_api'
    }
    reachable = isinstance(o.get('http_status'), int) and 200 <= o['http_status'] < 300
    identity = bool(record.get('store_name'))
    # The initial scan intentionally examines one page. Its sample size must
    # affect confidence, never be treated as a merchant catalog defect.
    bots = o.get('ai_bot_access') or {}
    crawler_access = bool(bots) and any(bool(value) for value in bots.values())
    discoverable = round(55 * quality + 10 * reachable + 10 * identity + 15 * machine_readable + 10 * crawler_access)
    caps = {a: 'Unverified' for a in ACTIONS}
    for action, observed in [('Discover products', n), ('Read product details', c['field_counts']['title']),
                             ('Read variants', any(p.get('variants') for p in record.get('products') or [])),
                             ('Read availability', c['field_counts']['availability'])]:
        if observed:
            caps[action] = 'Observed'
    u = o.get('ucp_analysis') or {}
    if u.get('checkout_capability'):
        caps['Checkout handoff'] = 'Declared'
    # Existing catalog probe is read-only; it does not execute cart or payment actions.
    probe = u.get('catalog_probe') or {}
    if probe.get('ok') is True and probe.get('product_count', 0) > 0:
        caps['Search products'] = 'Observed'
    elif probe.get('reason') == 'scanner agent profile URL is not configured as public HTTPS':
        caps['Search products'] = 'Scanner setup required'
    functional = round(100 * sum(v == 'Observed' for v in caps.values()) / len(ACTIONS))
    if c['public_source_complete']:
        confidence = 'Broad catalog evidence'
    elif c['coverage_percent'] is None:
        confidence = 'Limited sample; catalog total unknown'
    elif c['coverage_percent'] < 10:
        confidence = f"Limited sample: {c['coverage_percent']}% of the public source analyzed"
    elif c['coverage_percent'] < 60:
        confidence = f"Partial sample: {c['coverage_percent']}% of the public source analyzed"
    else:
        confidence = f"Broad sample: {c['coverage_percent']}% of the public source analyzed"
    missing = {field: n - count for field, count in c['field_counts'].items() if n and count < n}
    catalog_fix = (
        'Review the sampled products that are missing: ' + ', '.join(f'{count} {field}' for field, count in missing.items()) + '.'
        if missing else 'Sampled products have the required fields. Audit additional catalog pages before claiming full coverage.'
    )
    return {'version': VERSION, 'discoverable': discoverable if reachable else None, 'functional': functional if reachable else None,
            'protected': None, 'protection_status': 'Runtime protection unverified',
            'coverage': c, 'assessment_confidence': confidence, 'actions': caps,
            'recommendations': [
                catalog_fix if n else 'Publish accessible product information so agents can understand your catalog.',
                'Connect and test cart, shipping and checkout actions; declarations alone do not verify execution.',
                'Enable Auteric runtime authorization and validate policies before allowing agent actions.'],
            'platform_guidance': GUIDANCE.get(record.get('platform'), GUIDANCE['custom'])}
