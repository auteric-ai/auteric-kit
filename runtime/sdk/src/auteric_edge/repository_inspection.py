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
EXTENSIONS = {'.py', '.ts', '.tsx', '.js', '.jsx', '.json', '.toml', '.html'}


def operation_for(method, path, name):
    """Conservative behavior classification from HTTP verb + route + symbol."""
    terms = re.sub(r'([a-z])([A-Z])', r'\1 \2', name).lower() + ' ' + path.lower()
    cart = bool(re.search(r'cart|basket|bag', terms))
    product = bool(re.search(r'product|catalog|item', terms))
    checkout = 'checkout' in terms
    if re.search(r'capture|complete|payment|place.?order|refund', terms):
        return None
    if method == 'GET' and checkout: return 'get_checkout'
    if method == 'POST' and checkout: return 'create_checkout'
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
    candidates, observations, frameworks, skipped = [], [], set(), []
    count = total = 0
    truncated = False
    def candidate(method, route, symbol, source, line, authority):
        route = re.sub(r'<(?:(?:string|int|uuid):)?([A-Za-z_]\w*)>', r'{\1}', route)
        # Do not emit query strings, URLs or suspicious literal route values.
        if not route.startswith('/') or any(c in route for c in '?\n\r') or len(route) > 300:
            return
        operation = operation_for(method, route, symbol)
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
            candidates.append({'operation': operation, 'source': source, 'line': line, 'behavior': f'{method} {route}', 'function': symbol, 'confidence': 'requires_review', 'mapping': mapping, 'request_transformation': mapping.get('request', {}) if mapping else 'Inspect handler schema and canonical identifiers', 'response_transformation': 'Canonical identity response proposed; inspect fields and add explicit transforms if different', 'credential_reference': 'MERCHANT_API_TOKEN if required by reviewed authentication', 'unresolved': ['Authentication and buyer/cart ownership', 'Sellable product versus variant identity', 'Response fields, currency and authoritative state', 'Checkout must be handoff, never payment capture'], 'review_required': True})
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
                        for proposal in suggest_mappings(document):
                            candidates.append({**proposal, 'source': source, 'line': None, 'behavior': 'OpenAPI operation', 'review_required': True, 'request_transformation': proposal['mapping'].get('request', {}), 'response_transformation': proposal['mapping'].get('response', {}), 'credential_reference': 'Review securitySchemes; use local environment references only', 'unresolved': ['Response semantics and security scopes require review', 'Name matching alone does not prove merchant behavior']})
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
            if path.suffix in {'.js','.jsx','.ts','.tsx'}:
                for match in re.finditer(r'\b(?:app|router)\.(get|post|put|patch|delete)\(\s*[\'"]([^\'"\r\n]+)[\'"]', content):
                    candidate(match[1].upper(), match[2], 'route_handler', source, content[:match.start()].count('\n')+1, 'node_route_syntax')
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
    return {'mode': 'read_only_review_candidates', 'frameworks': sorted(frameworks), 'files_inspected': count, 'bytes_inspected': total, 'truncated': truncated, 'skipped_large_files': skipped, 'candidates': candidates, 'observations': observations, 'connector_config_template': {'base_url': '<MERCHANT_API_BASE_URL>', 'allowed_paths': sorted({m['path'] for m in rest}), 'credential_env': [], 'approved_mapping_digests': [mapping_digest(m) for m in rest]}, 'capability_coverage': [{'operation': op, 'status': 'candidate' if any(c['operation'] == op for c in candidates) else 'not_detected', 'evidence': [{'source': c['source'], 'line': c.get('line')} for c in candidates if c['operation'] == op]} for op in INPUTS], 'automatic_submission': False, 'application_modified': False, 'limitations': ['Source inspection never proves runtime behavior', 'Unmatched APIs need explicit developer mappings or a thin adapter', 'SDK clients and authentication locations are hints, not inferred permissions']}
