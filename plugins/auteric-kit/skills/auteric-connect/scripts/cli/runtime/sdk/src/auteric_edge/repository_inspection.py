"""Bounded read-only repository inspection. Evidence and candidates, never approval.

No imports, application execution, credential reads, source upload or file writes.
Framework patterns are hints; unsupported semantics are explicitly unresolved.
"""
import ast
import json
import os
import re
from pathlib import Path

from .mapping import suggest_mappings, mapping_digest, validate_mapping
from .models import INPUTS

SKIP = {'node_modules', 'vendor', 'dist', 'build', 'coverage', '__pycache__', 'tests', 'fixtures', 'test', 'secrets', 'credentials'}
EXTENSIONS = {
    '.py', '.ts', '.tsx', '.js', '.jsx', '.json', '.toml', '.html',
    '.php', '.rb', '.go', '.java', '.kt', '.cs', '.yaml', '.yml',
}

BLOCKED_TERMS = re.compile(r'admin|payment|refund|webhook|audit|internal|sandbox', re.I)
IDENTITY_TERMS = re.compile(r'auth|login|logout|register|session|\bme\b|profile|address|customer', re.I)
INFRASTRUCTURE_TERMS = re.compile(r'health|ready|status|metrics|meta/routes|openapi|swagger', re.I)
SHOPPING_SUPPORT_TERMS = re.compile(r'variant|categor|shipping|coupon|discount|wishlist|order|tracking|return', re.I)


def classify_api(method, path, name):
    """Classify every discovered API before selecting agent-shopping tools."""
    terms = f"{name} {path}".lower()
    if '/admin/' in path.lower() or BLOCKED_TERMS.search(terms):
        return {
            'domain': 'restricted',
            'shopping_relevance': 'excluded',
            'exposure': 'blocked_by_policy',
            'reason': 'Administrative, payment, refund, webhook or sandbox APIs are inventory only',
        }
    if INFRASTRUCTURE_TERMS.search(terms):
        return {
            'domain': 'infrastructure',
            'shopping_relevance': 'excluded',
            'exposure': 'inventory_only',
            'reason': 'Operational APIs are not shopping-agent tools',
        }
    if IDENTITY_TERMS.search(terms):
        return {
            'domain': 'identity',
            'shopping_relevance': 'supporting',
            'exposure': 'internal_dependency',
            'reason': 'Identity and ownership may support shopping actions but are not buyer-facing tools',
        }
    if re.search(r'product|catalog', terms):
        return {'domain': 'catalog', 'shopping_relevance': 'direct', 'exposure': 'tool_candidate',
                'reason': 'Catalog discovery is directly relevant to shopping agents'}
    if re.search(r'cart|basket|bag', terms):
        return {'domain': 'cart', 'shopping_relevance': 'direct', 'exposure': 'tool_candidate',
                'reason': 'Cart lifecycle is directly relevant to a purchase journey'}
    if 'checkout' in terms:
        return {'domain': 'checkout', 'shopping_relevance': 'direct', 'exposure': 'tool_candidate',
                'reason': 'Checkout lifecycle is directly relevant and requires runtime contract validation'}
    if 'order' in terms:
        return {'domain': 'order', 'shopping_relevance': 'direct', 'exposure': 'tool_candidate',
                'reason': 'Buyer-scoped order confirmation is directly relevant to a completed checkout'}
    if re.search(r'shipping|fulfillment|coupon|discount', terms):
        return {'domain': 'checkout_extension', 'shopping_relevance': 'direct', 'exposure': 'tool_candidate',
                'reason': 'Checkout extension requires an authoritative adapter and lifecycle validation'}
    if SHOPPING_SUPPORT_TERMS.search(terms):
        return {'domain': 'shopping_support', 'shopping_relevance': 'supporting', 'exposure': 'inventory_only',
                'reason': 'Shopping-related API is recorded but has no supported canonical tool contract'}
    return {'domain': 'other', 'shopping_relevance': 'excluded', 'exposure': 'inventory_only',
            'reason': 'No supported shopping-agent behavior was identified'}


def node_response_surface(content, match):
    """Separate obvious HTML page handlers from JSON/API routes without executing code."""
    tail = content[match.end():]
    boundary = re.search(
        r"\n\s*(?:[A-Za-z_$][\w$]*\.(?:get|post|put|patch|delete)\(\s*['\"]|"
        r"(?:registerRoute|route)\(\s*['\"](?:get|post|put|patch|delete)['\"])",
        tail,
        re.I,
    )
    handler = tail[:boundary.start()] if boundary else tail[:6000]
    if re.search(r"type\(\s*['\"]html|sendFile\s*\(|loadIndex\s*\(|render\s*\(", handler, re.I):
        return 'storefront_page'
    if re.search(r"\.json\s*\(|json\s*:", handler, re.I):
        return 'api'
    return 'api' if match.group(2).startswith('/api/') else 'ambiguous_http'


def operation_for(method, path, name):
    """Conservative behavior classification from HTTP verb + route + symbol."""
    terms = re.sub(r'([a-z])([A-Z])', r'\1 \2', name).lower() + ' ' + path.lower()
    classification = classify_api(method, path, name)
    if classification['exposure'] != 'tool_candidate':
        return None
    cart = bool(re.search(r'cart|basket|bag', terms))
    product = bool(re.search(r'product|catalog|item', terms))
    checkout = 'checkout' in terms
    order = 'order' in terms
    if method == 'GET' and order and re.search(r'[:{\[]', path) and not re.search(r'tracking|fulfillment|history', terms): return 'get_order'
    if method == 'GET' and re.search(r'shipping|fulfillment', terms): return 'get_shipping_options'
    if checkout and method in {'PUT', 'PATCH'} and re.search(r'address|destination', terms): return 'set_shipping_address'
    if checkout and method in {'PUT', 'PATCH'} and re.search(r'option|method|shipping|fulfillment', terms): return 'select_shipping_option'
    if cart and re.search(r'coupon|discount', terms):
        if method == 'DELETE': return 'remove_discount_code'
        if method in {'POST', 'PUT', 'PATCH'}: return 'apply_discount_code'
    if checkout and method in {'PUT', 'PATCH'}: return 'update_checkout'
    if checkout and method == 'DELETE': return 'cancel_checkout'
    if checkout and method == 'POST' and re.search(r'complete|confirm|place', terms): return 'complete_checkout'
    if checkout and method == 'POST' and re.search(r'cancel', terms): return 'cancel_checkout'
    if re.search(r'capture|payment|refund|coupon|discount|shipping|lock', terms):
        return None
    if method == 'GET' and checkout and re.search(r'[:{\[]', path): return 'get_checkout'
    if method == 'POST' and checkout and not re.search(r'[:{\[]', path): return 'create_checkout'
    if cart:
        if method == 'GET': return 'get_cart'
        if method == 'DELETE' and not product and 'line' not in terms: return 'cancel_cart'
        if method == 'POST' and 'cancel' in terms: return 'cancel_cart'
        if method == 'PUT' and re.search(r'items|lines|replace', terms): return 'replace_cart_items'
        if product or 'line' in terms:
            if method == 'POST': return 'add_to_cart'
            if method in {'PATCH', 'PUT'}: return 'update_cart_item'
            if method == 'DELETE': return 'remove_from_cart'
        if method == 'POST' and 'cancel' not in terms: return 'create_cart'
    if method == 'GET' and product:
        return 'get_product' if re.search(r'\{|:|\[|detail|lookup|get.?product', terms) else 'search_products'
    return None


def inspect_repository(project_root, *, max_files=1000, max_bytes=8_000_000):
    root = Path(project_root).absolute()
    if not root.is_dir() or any(p.is_symlink() for p in (root, *root.parents)):
        raise ValueError('Choose an existing non-symlink merchant repository')
    candidates, observations, api_inventory, frameworks, skipped = [], [], [], set(), []
    seen_apis = set()
    count = total = 0
    truncated = False
    def candidate(method, route, symbol, source, line, authority, surface='api'):
        route = re.sub(r'<(?:(?:string|int|uuid):)?([A-Za-z_]\w*)>', r'{\1}', route)
        # Do not emit query strings, URLs or suspicious literal route values.
        if not route.startswith('/') or any(c in route for c in '?\n\r') or len(route) > 300:
            return
        method = method.upper()
        operation = operation_for(method, route, symbol) if surface == 'api' else None
        classification = classify_api(method, route, symbol)
        if surface != 'api':
            classification = {
                'domain': 'storefront' if surface == 'storefront_page' else classification['domain'],
                'shopping_relevance': 'supporting' if surface == 'storefront_page' else classification['shopping_relevance'],
                'exposure': 'inventory_only',
                'reason': 'HTML storefront route is context, not an agent API' if surface == 'storefront_page' else 'Ambiguous HTTP route requires response-contract review',
            }
        api_key = (method, route, source, line)
        api = {
            'protocol': 'http', 'surface': surface, 'method': method, 'route': route, 'source': source, 'line': line,
            'kind': authority, 'symbol': symbol, **classification, 'canonical_candidate': operation,
            'tool_eligible': bool(operation),
        }
        if api_key not in seen_apis:
            api_inventory.append(api)
            seen_apis.add(api_key)
        observations.append({'source': source, 'line': line, 'kind': authority, 'symbol': symbol, 'method': method, 'route': route, 'canonical_candidate': operation})
        if operation:
            mapping = None
            # Read route templates can be represented directly. Request-body
            # semantics for writes cannot be inferred from route names alone.
            if method == 'GET':
                template = re.sub(r':([A-Za-z_]\w*)|\[([A-Za-z_]\w*)\]', lambda m: '{' + (m[1] or m[2]) + '}', route)
                parameters = re.findall(r'\{([A-Za-z_]\w*)\}', template)
                canonical_id = {'get_product':'product_id', 'get_cart':'cart_id', 'get_checkout':'checkout_id'}.get(operation)
                if not parameters or (len(parameters) == 1 and canonical_id):
                    request = {'path': {parameters[0]: {'source': canonical_id}}} if parameters else {}
                    try: mapping = validate_mapping({'operation': operation, 'method': method, 'path': template, 'request': request})
                    except ValueError: pass
            candidates.append({'operation': operation, 'tool_eligible': True, 'source': source, 'line': line, 'behavior': f'{method} {route}', 'function': symbol, 'confidence': 'requires_review', 'mapping': mapping, 'request_transformation': mapping.get('request', {}) if mapping else 'Inspect handler schema and canonical identifiers', 'response_transformation': 'Canonical identity response proposed; inspect fields and add explicit transforms if different', 'credential_reference': 'MERCHANT_API_TOKEN if required by reviewed authentication', 'unresolved': ['Authentication and buyer/cart ownership', 'Sellable product versus variant identity', 'Response fields, currency and authoritative state', 'Checkout must be handoff, never payment capture'], 'review_required': True})
    for directory, dirs, files in os.walk(root, followlinks=False):
        dirs[:] = sorted(d for d in dirs if not d.startswith('.') and d not in SKIP and not (Path(directory)/d).is_symlink())
        for name in sorted(files):
            path = Path(directory) / name
            if name.startswith('.') or path.suffix not in EXTENSIONS or re.search(r'secret|credential|\.lock|lockfile|token|private.?key', name, re.I) or path.is_symlink(): continue
            if count >= max_files or total >= max_bytes:
                truncated = True; break
            size = path.stat().st_size
            if size > 500_000 or total + size > max_bytes:
                skipped.append(str(path.relative_to(root))); continue
            content = path.read_text(encoding='utf-8', errors='replace')
            count += 1; total += size
            source = path.relative_to(root).as_posix()
            if name == 'package.json':
                try:
                    manifest = json.loads(content)
                    dependencies = {**manifest.get('dependencies', {}), **manifest.get('devDependencies', {})}
                    frameworks.update(f for f in ('next', 'express', 'vite') if f in dependencies)
                except (ValueError, TypeError): pass
            if path.suffix == '.json':
                try:
                    document = json.loads(content)
                    if isinstance(document, dict) and ('openapi' in document or 'swagger' in document):
                        proposals = {(item['mapping']['method'], item['mapping']['path']): item for item in suggest_mappings(document)}
                        for route, path_item in document.get('paths', {}).items():
                            if not isinstance(path_item, dict):
                                continue
                            for method, operation_spec in path_item.items():
                                upper = method.upper()
                                if upper not in {'GET', 'POST', 'PUT', 'PATCH', 'DELETE'} or not isinstance(operation_spec, dict):
                                    continue
                                before = len(candidates)
                                candidate(upper, route, operation_spec.get('operationId', 'openapi_operation'), source, None, 'openapi_document')
                                proposal = proposals.get((upper, route))
                                if proposal and len(candidates) > before:
                                    candidates[-1].update(
                                        mapping=proposal['mapping'],
                                        request_transformation=proposal['mapping'].get('request', {}),
                                        response_transformation=proposal['mapping'].get('response', {}),
                                        credential_reference='Review securitySchemes; use local environment references only',
                                        unresolved=['Response semantics and security scopes require review', 'Name matching alone does not prove merchant behavior'],
                                    )
                except (ValueError, TypeError, AttributeError): pass
            if path.suffix == '.py':
                try: tree = ast.parse(content)
                except SyntaxError: continue
                imports = {node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
                if 'fastapi' in imports: frameworks.add('fastapi')
                if 'flask' in imports: frameworks.add('flask')
                for node in ast.walk(tree):
                    if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)): continue
                    for decorator in node.decorator_list:
                        if not isinstance(decorator, ast.Call) or not isinstance(decorator.func, ast.Attribute) or not decorator.args: continue
                        route = decorator.args[0]
                        if not isinstance(route, ast.Constant) or not isinstance(route.value, str): continue
                        method = decorator.func.attr.upper()
                        methods = [method] if method in {'GET','POST','PUT','PATCH','DELETE'} else []
                        if method == 'ROUTE':
                            methods = ['GET']
                            for keyword in decorator.keywords:
                                if keyword.arg == 'methods' and isinstance(keyword.value, (ast.List, ast.Tuple)):
                                    methods = [v.value.upper() for v in keyword.value.elts if isinstance(v, ast.Constant) and isinstance(v.value, str)]
                        for method in methods: candidate(method, route.value, node.name, source, node.lineno, 'python_route_ast')
            if path.suffix == '.php':
                if 'Route::' in content: frameworks.add('laravel')
                for match in re.finditer(r"\bRoute::(get|post|put|patch|delete)\(\s*['\"]([^'\"\r\n]+)['\"]", content, re.I):
                    candidate(match[1], match[2], 'route_handler', source, content[:match.start()].count('\n') + 1, 'php_route_syntax')
            if path.suffix == '.rb':
                if re.search(r'Rails\.application\.routes|resources\s+|namespace\s+', content): frameworks.add('rails')
                for match in re.finditer(r"^\s*(get|post|put|patch|delete)\s+['\"]([^'\"\r\n]+)['\"]", content, re.I | re.M):
                    candidate(match[1], match[2], 'route_handler', source, content[:match.start()].count('\n') + 1, 'ruby_route_syntax')
            if path.suffix == '.go':
                if re.search(r'gin-gonic|labstack/echo|gorilla/mux', content): frameworks.add('go-http')
                for match in re.finditer(r"\b[A-Za-z_]\w*\.(GET|POST|PUT|PATCH|DELETE)\(\s*['\"]([^'\"\r\n]+)['\"]", content):
                    candidate(match[1], match[2], 'route_handler', source, content[:match.start()].count('\n') + 1, 'go_route_syntax')
            if path.suffix in {'.java', '.kt'}:
                if re.search(r'org\.springframework|@(RestController|Controller)', content): frameworks.add('spring')
                for match in re.finditer(r"@(Get|Post|Put|Patch|Delete)Mapping\(\s*(?:value\s*=\s*)?['\"]([^'\"\r\n]+)['\"]", content):
                    candidate(match[1], match[2], 'route_handler', source, content[:match.start()].count('\n') + 1, 'spring_route_syntax')
            if path.suffix == '.cs':
                if re.search(r'\[ApiController\]|Microsoft\.AspNetCore', content): frameworks.add('aspnet')
                for match in re.finditer(r"\[Http(Get|Post|Put|Patch|Delete)\(\s*['\"]([^'\"\r\n]+)['\"]\s*\)\]", content):
                    route = match[2] if match[2].startswith('/') else '/' + match[2]
                    candidate(match[1], route, 'route_handler', source, content[:match.start()].count('\n') + 1, 'aspnet_route_syntax')
            if path.suffix in {'.js','.jsx','.ts','.tsx'}:
                for match in re.finditer(r'\b[A-Za-z_$][\w$]*\.(get|post|put|patch|delete)\(\s*[\'"]([^\'"\r\n]+)[\'"]', content, re.I):
                    candidate(match[1].upper(), match[2], 'route_handler', source, content[:match.start()].count('\n')+1, 'node_route_syntax', node_response_surface(content, match))
                for match in re.finditer(r'\broute\(\s*[\'"](get|post|put|patch|delete)[\'"]\s*,\s*[\'"]([^\'"\r\n]+)[\'"]', content):
                    candidate(match[1].upper(), match[2], 'route_handler', source, content[:match.start()].count('\n')+1, 'node_route_helper_syntax')
                for match in re.finditer(r'\bregisterRoute\(\s*[\'"](get|post|put|patch|delete)[\'"]\s*,\s*[\'"]([^\'"\r\n]+)[\'"]', content):
                    candidate(match[1].upper(), match[2], 'route_handler', source, content[:match.start()].count('\n')+1, 'node_route_register_helper')
                # Recognize literal path lists used with a small Express route
                # helper. Dynamic route construction remains out of scope.
                for loop in re.finditer(r'for\s*\(\s*const\s+(\w+)\s+of\s+\[([^\]]*)\]\s*\)\s*\{([\s\S]{0,12000}?)\}', content):
                    variable, values, body = loop.groups()
                    routes = re.findall(r'[\'"]([^\'"\r\n]+)[\'"]', values)
                    for call in re.finditer(r'\broute\(\s*[\'"](get|post|put|patch|delete)[\'"]\s*,\s*' + re.escape(variable) + r'(?=\s*[,+])', body):
                        suffix = body[call.end():].lstrip()
                        suffix_match = re.match(r'\+\s*[\'"]([^\'"\r\n]*)[\'"]', suffix)
                        for route in routes:
                            candidate(call[1].upper(), route + (suffix_match[1] if suffix_match else ''), 'route_handler', source, content[:loop.start() + call.start()].count('\n')+1, 'node_route_helper_loop')
                    # The first handler body can contain braces, so the bounded
                    # loop body above may end before a later `base + '/:id'`
                    # handler. Search the same source for that literal form.
                    for call in re.finditer(r'\broute\(\s*[\'"](get|post|put|patch|delete)[\'"]\s*,\s*' + re.escape(variable) + r'\s*\+\s*[\'"]([^\'"\r\n]+)[\'"]', content):
                        for route in routes:
                            candidate(call[1].upper(), route + call[2], 'route_handler', source, content[:call.start()].count('\n')+1, 'node_route_helper_loop')
                if name in {'route.ts','route.js'} and 'app' in path.relative_to(root).parts:
                    parts = path.relative_to(root).parts
                    route = '/' + '/'.join(parts[parts.index('app')+1:-1])
                    for match in re.finditer(r'export\s+(?:async\s+)?function\s+(GET|POST|PUT|PATCH|DELETE)\b', content):
                        candidate(match[1], route, match[1], source, content[:match.start()].count('\n')+1, 'next_route_convention')
            # Emit only locations/types, not arbitrary source code or string values.
            for kind, pattern in [('browser_only_cart', r'\blocalStorage\b'), ('simulated_payment', r'\bfakePayment\b'), ('authentication', r'\b(?:Depends|authenticate|requireAuth|verifyToken|login_required)\b'), ('commerce_service', r'\b(?:class|function|def)\s+\w*(?:Product|Cart|Checkout|Order|product|cart|checkout|order)\w*')]:
                match = re.search(pattern, content)
                if match: observations.append({'source': source, 'line': content[:match.start()].count('\n')+1, 'kind': kind, 'requires_manual_trace': True})
        if truncated: break
    rest = [c['mapping'] for c in candidates if c.get('mapping')]
    domain_counts = {}
    for item in api_inventory:
        domain_counts[item['domain']] = domain_counts.get(item['domain'], 0) + 1
    inventory_summary = {
        'total_endpoints': len(api_inventory),
        'api_endpoints': sum(item['surface'] == 'api' for item in api_inventory),
        'storefront_routes': sum(item['surface'] == 'storefront_page' for item in api_inventory),
        'ambiguous_http_routes': sum(item['surface'] == 'ambiguous_http' for item in api_inventory),
        'candidate_endpoints': sum(item['tool_eligible'] for item in api_inventory),
        'tool_candidates': len({item['canonical_candidate'] for item in api_inventory if item['tool_eligible']}),
        'shopping_supporting': sum(item['shopping_relevance'] == 'supporting' for item in api_inventory),
        'blocked_by_policy': sum(item['exposure'] == 'blocked_by_policy' for item in api_inventory),
        'inventory_only': sum(item['exposure'] == 'inventory_only' for item in api_inventory),
        'domains': dict(sorted(domain_counts.items())),
    }
    return {
        'mode': 'full_api_inventory_filtered_commerce_tools',
        'frameworks': sorted(frameworks), 'files_inspected': count, 'bytes_inspected': total,
        'truncated': truncated, 'skipped_large_files': skipped,
        'api_inventory': api_inventory, 'inventory_summary': inventory_summary,
        'candidates': candidates, 'observations': observations,
        'connector_config_template': {'base_url': '<MERCHANT_API_BASE_URL>', 'allowed_paths': sorted({m['path'] for m in rest}), 'credential_env': [], 'approved_mapping_digests': [mapping_digest(m) for m in rest]},
        'capability_coverage': [{'operation': op, 'status': 'candidate' if any(c['operation'] == op for c in candidates) else 'not_detected', 'evidence': [{'source': c['source'], 'line': c.get('line')} for c in candidates if c['operation'] == op]} for op in INPUTS],
        'automatic_submission': False, 'application_modified': False,
        'limitations': ['Source inspection never proves runtime behavior', 'Every detected API is inventoried, but only supported shopping operations become tool candidates', 'Unmatched APIs need explicit developer mappings or a thin adapter', 'SDK clients and authentication locations are hints, not inferred permissions'],
    }
