"""Durable operator journeys. Approval resumes the same request; ambiguous writes never retry."""
import asyncio
import json
import time

from fastapi import Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from .storage import digest, encode, uid


class ConnectionInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    # Explicit owner request; ordinary runs retain their enabled-operation gate.
    bootstrap_sidecar: bool = False
    product_id: str | None = Field(default=None, min_length=1, max_length=200)
    query: str = Field(default='', max_length=500)
    # Optional merchant-known code for the discount stages; defaults to a
    # clearly synthetic code that honest merchants may refuse.
    discount_code: str = Field(default='AUTERIC-CONNECTION-TEST', min_length=1, max_length=200)


# Payment-terminal operations are never executed by a connection test: checkout
# completion requires a configured payment handler and buyer authorization, and
# an order read requires a completed payment. Their coverage is the current
# activation evidence plus a fresh policy evaluation of the exact request.
EVIDENCE_ONLY = frozenset({'complete_checkout', 'get_order'})


def checkout_identity(result):
    """Accept the canonical Native HTTP id and the legacy worker id shape."""
    if not isinstance(result, dict):
        return None
    return result.get('checkout_id') or result.get('id')


def attach_connection_runs(app, dbs, execute):
    state = app.state
    root = '/api/commerce/stores/{store_id}'

    def binding(store_id):
        store = state.store_row(store_id)
        with dbs.db() as db:
            mappings = [[row['operation'], row['id']] for row in db.execute("SELECT mappings.operation,mappings.id FROM mappings JOIN capability_controls ON mappings.store=capability_controls.store AND mappings.operation=capability_controls.operation WHERE mappings.store=? AND mappings.state='active' AND capability_controls.enabled=1 ORDER BY mappings.operation", (store_id,))]
        with dbs.db() as db:
            installation=db.execute('SELECT id,transport,trust_binding,revoked_at FROM installations WHERE store_id=? AND environment=?',
                (store_id,store['environment'])).fetchone()
        with dbs.db() as db:
            capabilities = [dict(row) for row in db.execute('SELECT operation,enabled,revision FROM capability_policies WHERE store_id=? ORDER BY operation', (store_id,))] if installation and installation['transport'] == 'native_http' else []
        return digest(encode({'capabilities': capabilities, 'mappings': mappings, 'policy': store['policy'], 'release': store['connector_release'],
            'environment': store['environment'],'installation':dict(installation) if installation else None}))

    state.connection_binding = binding

    def get(store_id, run_id):
        with dbs.db() as db:
            row = db.execute('SELECT * FROM connection_runs WHERE id=? AND store=?', (run_id, store_id)).fetchone()
        if not row:
            raise HTTPException(404, 'Connection run not found')
        return dict(row), json.loads(row['body'])

    @app.get(root + '/test-products')
    async def test_products(store_id: str, actor=Depends(state.user_dependency)):
        """Read a bounded list of sellable products for an operator test.

        This uses the currently enabled merchant mapping, never Scanner data or
        a remembered product id.  It is deliberately read-only and exposes no
        buyer/session context.
        """
        state.owned(store_id, actor)
        if not state.operation_enabled(store_id, 'search_products'):
            raise HTTPException(409, 'Enable tested catalog search before selecting a Connection Test product')
        mapping = state.selected_mapping(store_id, operation='search_products')
        # The locked Native HTTP contract requires a non-empty `q`. STRIDE
        # trims this bounded, read-only whitespace discovery query to its
        # normal catalog listing; a selected product test uses its title below.
        result = await state.dispatch(store_id, 'search_products', {'query': ' ', 'limit': 20}, mapping,
                                      principal='operator-catalog:' + actor['id'][:32], policy_exempt=True)
        rows = result.get('results', []) if isinstance(result, dict) else result
        if not isinstance(rows, list):
            raise HTTPException(502, 'Merchant catalog search returned an invalid product list')
        products = []
        for product in rows[:20]:
            if not isinstance(product, dict):
                continue
            product_id = product.get('product_id') or product.get('id')
            variants = product.get('variants') or []
            sellable = any(isinstance(v, dict) and v.get('available', v.get('availability') != 'out_of_stock') for v in variants)
            if not isinstance(product_id, str) or not product_id or not sellable:
                continue
            price = product.get('price') or next((v.get('price') for v in variants if isinstance(v, dict) and isinstance(v.get('price'), dict)), None)
            products.append({
                'product_id': product_id,
                'title': product.get('title') or product.get('name') or product_id,
                'description': product.get('description') or '',
                'price': price if isinstance(price, dict) else None,
            })
        if not products:
            raise HTTPException(409, 'No sellable products were returned by the current merchant catalog')
        return {'products': products, 'selected_product_id': products[0]['product_id']}

    def persist(report):
        report['updated_at'] = time.time()
        with dbs.db() as db:
            db.execute('UPDATE connection_runs SET binding=?,state=?,body=?,updated=? WHERE id=? AND store=?', (report['binding'], report['state'], encode(report), report['updated_at'], report['run_id'], report['store_id']))

    def record_sidecar_runtime_evidence(report):
        """Bind a passed live journey to the exact generated sidecar profiles.

        This runs only after every executable stage has a merchant receipt and
        the Native Phase 1 adversarial probes pass. Merely registering a
        profile or running project tests can never create this evidence.
        """
        passed_operations = {
            step['operation'] for step in report.get('stages', [])
            if step.get('state') == 'passed' and not step.get('evidence_only')
        }
        if not passed_operations:
            return []
        writes = passed_operations - {'search_products', 'get_product', 'get_cart', 'get_checkout'}
        if writes and report.get('phase1_probes', {}).get('passed') is not True:
            return []
        with dbs.db() as db:
            installation = db.execute(
                'SELECT id,environment,trust_binding FROM installations '
                'WHERE store_id=? AND environment=? AND transport=? AND revoked_at IS NULL',
                (report['store_id'], report['environment'], 'native_http'),
            ).fetchone()
            if not installation:
                return []
            try:
                binding = json.loads(installation['trust_binding'] or '{}')
                profiles = binding['sidecar']['profiles']
            except (KeyError, TypeError, ValueError):
                return []
            rows = {
                row['operation']: dict(row)
                for row in db.execute(
                    'SELECT operation,binding_digest FROM installation_operations '
                    'WHERE installation_id=?', (installation['id'],)
                )
            }
            passed_at = time.time()
            recorded = []
            for operation in sorted(passed_operations & set(profiles) & set(rows)):
                mapping_fingerprint = profiles[operation].get('mapping_fingerprint')
                contract_fingerprint = rows[operation]['binding_digest']
                if not isinstance(mapping_fingerprint, str) or not mapping_fingerprint.startswith('sha256:'):
                    continue
                evidence = {
                    'kind': 'sidecar_runtime', 'status': 'pass',
                    'evidence_id': 'evidence_' + digest(report['run_id'] + ':' + operation)[:24],
                    'installation_id': installation['id'], 'operation': operation,
                    'environment': installation['environment'],
                    'mapping_fingerprint': mapping_fingerprint,
                    'contract_fingerprint': contract_fingerprint,
                    'passed_at': passed_at, 'expires_at': min(passed_at + 86400, report.get('verification_expires_at', float('inf'))),
                    'test_suites': ['connection_test', 'sidecar_runtime', 'bridge_contract'],
                    'connection_run_id': report['run_id'],
                }
                db.execute(
                    'UPDATE installation_operations SET test_evidence=? '
                    'WHERE installation_id=? AND operation=?',
                    (encode(evidence), installation['id'], operation),
                )
                if report.get('bootstrap_sidecar'):
                    stage = next(step for step in report['stages'] if step['operation'] == operation and step['state'] == 'passed')
                    receipt = stage.get('merchant_receipt') or {}
                    if receipt.get('installation_id') != installation['id'] or receipt.get('operation') != operation:
                        raise HTTPException(409, 'Deployment evidence requires the exact installed merchant receipt')
                    db.execute('UPDATE installation_operations SET deployment_evidence=? WHERE installation_id=? AND operation=?',
                        (encode({'kind': 'observed_native_receipt', 'deployed_at': passed_at,
                                 'connection_run_id': report['run_id'], 'action_id': receipt['action_id'],
                                 'binding_digest': contract_fingerprint}), installation['id'], operation))
                recorded.append(operation)
        if recorded:
            dbs.event(
                state.store_row(report['store_id'])['org'], report['store_id'], report['actor'],
                'sidecar.runtime_evidence_recorded',
                {'run_id': report['run_id'], 'operations': recorded},
            )
        return recorded

    async def drive_in_background(report):
        """Keep the operator request short while preserving exact live progress.

        A connection journey calls real merchant APIs and can legitimately take
        longer than an edge/browser request timeout. Its durable report is the
        source of truth; the Console polls that report rather than treating an
        abandoned HTTP response as an unknown merchant outcome.
        """
        try:
            await drive(report)
        except Exception:
            # We do not retry here: an unexpected worker interruption after a
            # write is ambiguous. Persist an explicit review state instead of
            # leaving a misleading forever-pending run.
            report.update(
                state='uncertain',
                failure='The test worker stopped before a final merchant receipt was recorded. Inspect Activity and receipts; do not retry automatically.',
            )
            persist(report)

    def schedule(report):
        task = asyncio.create_task(drive_in_background(report))
        tasks = getattr(app.state, 'connection_test_tasks', set())
        tasks.add(task)
        app.state.connection_test_tasks = tasks
        task.add_done_callback(tasks.discard)

    async def drive(report):
        sid, run_id = report['store_id'], report['run_id']
        values = report['values']
        for step in report['stages']:
            if step['state'] == 'passed':
                continue
            op = step['operation']
            native_http = bool(report.get('native_http'))
            data = {'query': report['query'], 'limit': 1} if op == 'search_products' else {'product_id': report['product_id']} if op == 'get_product' else {'currency': values.get('currency', 'USD'), 'items': []} if op == 'create_cart' else {'cart_id': values.get('cart_id')}
            if op in {'add_to_cart', 'update_cart_item', 'remove_from_cart'}:
                data['product_id'] = report['product_id']
                if op == 'add_to_cart' and values.get('variant_id'):
                    data['variant_id'] = values['variant_id']
                if op in {'update_cart_item', 'remove_from_cart'}:
                    line_id = values.get('line_id')
                    if line_id:
                        data['line_id'] = line_id
                    revision = values.get('cart', {}).get('resource_revision')
                    if isinstance(revision, int):
                        data['expected_revision'] = revision
                if op != 'remove_from_cart': data['quantity'] = 1
            if op == 'replace_cart_items':
                item = {'product_id': report['product_id'], 'quantity': 1}
                if values.get('variant_id'):
                    item['variant_id'] = values['variant_id']
                data['items'] = [item]
            if op in {'apply_discount_code', 'remove_discount_code'}:
                data['code'] = report['discount_code']
            if op == 'get_checkout': data = {'checkout_id': values.get('checkout_id')}
            if op == 'set_shipping_address':
                data = {'checkout_id': values.get('checkout_id'), 'address': {
                    'line1': '1 Connection Test Way', 'city': 'Sandbox',
                    ('country_code' if native_http else 'country'): 'US', 'postal_code': '00000'}}
            if op == 'select_shipping_option':
                data = {'checkout_id': values.get('checkout_id'), 'option_id': values.get('shipping_option_id')}
            if op == 'update_checkout':
                if native_http:
                    # MEP/1 does not permit changing checkout line items.
                    # Exercise a real supported mutation instead of sending
                    # the old Gateway ``items`` shape, which a Native bridge
                    # must reject (or worse, ignore).
                    data = {'checkout_id': values.get('checkout_id'), 'shipping_address': {
                        'line1': '2 Connection Test Way', 'city': 'Sandbox',
                        'country_code': 'US', 'postal_code': '00000'}}
                    revision = values.get('checkout', {}).get('resource_revision')
                    if isinstance(revision, int):
                        data['expected_revision'] = revision
                else:
                    item = {'product_id': report['product_id'], 'quantity': 1}
                    if values.get('variant_id'):
                        item['variant_id'] = values['variant_id']
                    data = {'checkout_id': values.get('checkout_id'), 'items': [item]}
            if op == 'cancel_checkout':
                data = {'checkout_id': values.get('checkout_id')}
            if op == 'complete_checkout':
                data = {'checkout_id': values.get('checkout_id'), 'payment': {}}
            if op == 'get_order':
                data = {'order_id': 'connection-test-payment-never-executed'}
            if step.get('request') is not None and step['request'] != data:
                raise HTTPException(409, 'Resume request differs from original reviewed request')
            step.update(request=data, state='running', started_at=time.time())
            report['state'] = 'running'
            persist(report)
            started = time.monotonic()
            if step.get('evidence_only'):
                try:
                    store = state.store_row(sid)
                    mapping = state.selected_mapping(sid, operation=op)
                    state.validate_runtime_mapping(store, mapping)
                    from .adapter_manifest import manifest_for
                    tests = json.loads(mapping['tests'] or 'null')
                    if (
                        not tests
                        or tests.get('status') != 'pass'
                        or tests.get('fingerprint') != mapping['fingerprint']
                        or tests.get('environment') != store['environment']
                        or tests.get('tested_at', 0) < time.time() - 86400
                        or tests.get('adapter_manifest', {}).get('digest') != manifest_for(op, mapping['fingerprint'])['digest']
                    ):
                        raise HTTPException(409, 'Current activation evidence for this payment-terminal operation is required')
                    policy = state.policy_class.model_validate_json(store['policy'])
                    # Mirror the gateway's authoritative product context: cart
                    # lines that name a variant resolve against the parent read.
                    products = {}
                    product = values.get('product')
                    if isinstance(product, dict):
                        products[product['id']] = product
                        for variant in product.get('variants', []):
                            if variant.get('id'):
                                products[variant['id']] = {
                                    **product, **variant, 'id': variant['id'],
                                    'metadata': {**product.get('metadata', {}), **variant.get('metadata', {}),
                                                 'parent_product_id': product['id']},
                                }
                    decision = state.evaluate_policy(op, data, policy, {
                        'agent_verified': True, 'session_id': 'connection-test:' + run_id,
                        'agent_credential_id': 'operator:' + report['actor'], 'operator_test': True,
                        'products': products,
                        'cart': values.get('cart'), 'checkout': values.get('checkout'),
                    })
                    if decision['outcome'] == 'BLOCK':
                        raise HTTPException(409, 'Storefront policy would block this operation: ' + '; '.join(decision['reasons']))
                    step.update(state='passed', result=None, gateway='evaluated_not_executed',
                                connector='payment_never_executed', merchant_receipt=None,
                                policy_decision=decision,
                                evidence={'kind': 'activation_evidence', 'fingerprint': mapping['fingerprint']},
                                latency_ms=round((time.monotonic()-started)*1000, 1), finished_at=time.time())
                    persist(report)
                except Exception as exc:
                    step.update(state='failed', failure=exc.detail if isinstance(exc, HTTPException) and isinstance(exc.detail, str) else 'Execution failed; inspect audit and receipt before starting a new run', finished_at=time.time(), latency_ms=round((time.monotonic()-started)*1000, 1))
                    report['state'] = 'failed'
                    persist(report)
                    return report
                continue
            try:
                if report.get('bootstrap_sidecar'):
                    envelope = await state.execute_sidecar_verification(report, op, data, run_id + ':' + step['name'])
                else:
                    envelope = await execute(sid, op, data, 'operator:' + report['actor'], 'connection-test:' + run_id, run_id + ':' + step['name'], operator_id=report['actor'])
                step.update(action_id=envelope['action_id'], latency_ms=round((time.monotonic()-started)*1000, 1), finished_at=time.time(), policy_decision=envelope.get('decision'), approval_digest=envelope.get('digest'))
                if envelope['state'] != 'executed':
                    step['state'] = envelope['state']
                    report['state'] = envelope['state']
                    persist(report)
                    return report
                result = envelope['result']
                with dbs.db() as db:
                    traffic = db.execute('SELECT decision FROM traffic WHERE id=? AND store=?', (envelope['action_id'], sid)).fetchone()
                    receipt = db.execute('SELECT id,state,mapping_version FROM jobs WHERE id=? AND store=?', (envelope['action_id'], sid)).fetchone()
                    if native_http:
                        from .execution_receipts import gateway_receipt
                        receipt = gateway_receipt(db, sid, envelope['action_id'])
                        if not receipt:
                            raise HTTPException(502, 'Native execution has no authority-bound merchant receipt')
                step.update(state='passed', result=result, gateway='allowed', connector='confirmed', merchant_receipt=dict(receipt) if receipt else None, policy_decision=json.loads(traffic['decision']) if traffic else step['policy_decision'])
                if op == 'get_product':
                    # Native HTTP returns the canonical product document
                    # (product_id + price objects), while the legacy worker
                    # fixture returns its historical `currency` field.
                    values['currency'] = (
                        result.get('currency')
                        or next((item.get('price', {}).get('currency') for item in result.get('variants', [])
                                 if isinstance(item, dict) and isinstance(item.get('price'), dict)), None)
                        or 'USD'
                    )
                    values['product'] = result
                    variants = result.get('variants', [])
                    if variants:
                        selected = next((variant for variant in variants
                                         if variant.get('available', variant.get('availability', 'unknown') != 'out_of_stock')
                                         and (variant.get('inventory') is None or variant.get('inventory') > 0)), None)
                        variant_id = selected.get('id') if selected else None
                        variant_id = variant_id or (selected.get('variant_id') if selected else None)
                        if not selected or not variant_id:
                            raise HTTPException(409, 'The selected test product has no purchasable variant')
                        values['variant_id'] = variant_id
                if op == 'create_cart':
                    values['cart_id'] = result.get('cart_id') or result['id']
                if op == 'create_checkout':
                    checkout_id = checkout_identity(result)
                    if not checkout_id:
                        raise HTTPException(502, 'Merchant checkout response has no canonical checkout identifier')
                    values.update(checkout_id=checkout_id, checkout_status=result['status'], checkout_cart_id=values['cart_id'])
                # Latest authoritative cart/checkout snapshots for the
                # evidence-stage policy evaluations below.
                if isinstance(result, dict):
                    if 'items' in result and result.get('id') == values.get('cart_id'):
                        values['cart'] = result
                    if 'line_items' in result and result.get('cart_id') == values.get('cart_id'):
                        values['cart'] = result
                        lines = result.get('line_items') or []
                        if lines and lines[0].get('line_id'):
                            values['line_id'] = lines[0]['line_id']
                    if op in {'create_checkout', 'get_checkout', 'update_checkout', 'cancel_checkout'} and result.get('cart_id') and checkout_identity(result) == values.get('checkout_id'):
                        values['checkout'] = result
                if op == 'get_shipping_options' and result:
                    values['shipping_option_id'] = result[0]['id']
                persist(report)
            except Exception as exc:
                import logging
                logging.getLogger(__name__).error('Connection Test stage failed: %s (%s)', step['name'], type(exc).__name__)
                action_id = digest(sid + 'operator:' + report['actor'] + 'connection-test:' + run_id + run_id + ':' + step['name'])
                with dbs.db() as db:
                    job = db.execute('SELECT state FROM jobs WHERE id=? AND store=?', (action_id, sid)).fetchone()
                    if native_http:
                        native = db.execute('SELECT outcome FROM execution_actions WHERE action_id=? AND store_id=?', ('action_' + action_id, sid)).fetchone()
                        job = {'state': native['outcome']} if native else job
                uncertain = op not in {'search_products', 'get_product', 'get_cart', 'get_checkout'} and (
                    (isinstance(exc, HTTPException) and exc.status_code == 504)
                    or (job and job['state'] in {'claimed', 'reserved', 'executing', 'uncertain', 'completed', 'reconciled'}))
                step.update(state='uncertain' if uncertain else 'failed', action_id=action_id, failure=exc.detail if isinstance(exc, HTTPException) and isinstance(exc.detail, str) else 'Execution failed; inspect audit and receipt before starting a new run', finished_at=time.time(), latency_ms=round((time.monotonic()-started)*1000, 1))
                report['state'] = step['state']
                persist(report)
                return report
        report.update(state='passed', result={**values, 'product_id': report['product_id']})
        from .native_phase1_probes import verify as verify_native_phase1
        report['phase1_probes'] = await verify_native_phase1(app, report)
        if report.get('bootstrap_sidecar') and not report['phase1_probes'].get('passed'):
            report.update(state='failed', failure=report['phase1_probes'].get('reason', 'Live safety probes failed'))
            persist(report)
            return report
        if report['phase1_probes'].get('passed'):
            # The existing remote-disable probe restores the same enabled state
            # with a new revision. Bind evidence to that restored configuration.
            report['binding'] = binding(sid)
        report['sidecar_runtime_evidence'] = record_sidecar_runtime_evidence(report)
        if report.get('bootstrap_sidecar'):
            from .installations import set_capability_policy
            from .capabilities import operation_states
            preflight = [operation_states(dbs, state.store_row(sid), operation)
                         for operation in report['sidecar_runtime_evidence']]
            if any(item.get('reason') not in {None, 'disabled_by_merchant'} for item in preflight):
                report.update(state='failed', failure='Existing capability preflight rejected the verified installation')
                persist(report)
                return report
            for operation in report['sidecar_runtime_evidence']:
                set_capability_policy(dbs, sid, operation, True, report['actor'])
            report['binding'] = binding(sid)
        report['evidence_observed_at'] = time.time()
        # Persist the finished evidence before evaluating activation. Connection
        # Health reads the durable current-binding run; it must never activate
        # from an in-memory or partially completed journey.
        persist(report)
        try:
            activation = state.activate_verified_agent_access(sid, report['actor'], automatic=True)
            report['activation'] = {
                'state': 'active',
                'agent_access_enabled': True,
                'message': 'Protected Agent Access activated automatically from this verified Connection Test.',
                **activation,
            }
        except HTTPException as exc:
            report['activation'] = {
                'state': 'needs_action',
                'agent_access_enabled': False,
                'message': exc.detail if isinstance(exc.detail, str) else 'Connection evidence requires review before activation.',
            }
        persist(report)
        # Scanner receives a short-lived receipt only after the whole
        # operator-authorized journey passes and Agent Access activates. A
        # publishing failure is recorded separately and never rewrites the
        # merchant execution result.
        report['scanner_evidence'] = await state.publish_scanner_evidence(state.store_row(sid), report)
        persist(report)
        return report

    @app.post(root + '/test-transaction')
    async def start(store_id: str, body: ConnectionInput = ConnectionInput(), background: bool = False, actor=Depends(state.user_dependency)):
        store = state.owned(store_id, actor)
        with dbs.db() as db:
            installation = db.execute(
                'SELECT transport FROM installations WHERE store_id=? AND environment=? AND revoked_at IS NULL',
                (store_id, store['environment']),
            ).fetchone()
        native_http = bool(installation and installation['transport'] == 'native_http')
        ops = {op for op in state.inputs if state.operation_enabled(store_id, op)}
        direct = None
        if body.bootstrap_sidecar:
            from .direct_verification import direct_installation, OPERATIONS
            direct = direct_installation(dbs, store)
            if not direct or not store['policy_reviewed_at']:
                raise HTTPException(409, 'A reviewed non-production Sidecar installation is required')
            profiles = json.loads(direct['trust_binding'])['sidecar']['profiles']
            if not set(OPERATIONS) <= set(profiles):
                raise HTTPException(409, 'The five minimum catalog/cart mappings are required')
            # Additional stronger profiles are preserved, never enabled by this run.
            ops = set(OPERATIONS)
        if store['environment'] == 'production':
            # Production connection tests remain read-only for every legacy
            # transport.  A verified Native HTTP installation is the narrowly
            # scoped exception: it creates an isolated pairwise-owned cart and
            # executes only the selected non-payment Phase 1 operations.
            if not native_http:
                ops &= {'search_products', 'get_product'}
        if not ops:
            raise HTTPException(409, 'Enable tested mappings before a Connection Test')
        writes = ops - {'search_products', 'get_product', 'get_cart', 'get_checkout'}
        if (writes or 'get_product' in ops) and not body.product_id:
            raise HTTPException(422, 'Select a known safe test product_id explicitly')
        if writes and not {'get_product', 'create_cart', 'get_cart'} <= ops:
            raise HTTPException(409, 'Write journeys require enabled product lookup, cart creation and cart reads')
        if 'get_checkout' in ops and 'create_checkout' not in ops:
            raise HTTPException(409, 'Checkout lookup test requires checkout handoff')
        if ops & {'get_cart', 'add_to_cart', 'update_cart_item', 'remove_from_cart', 'replace_cart_items', 'cancel_cart', 'create_checkout'} and 'create_cart' not in ops:
            raise HTTPException(409, 'Select an isolated cart creation capability for this journey')
        order = [('product_search','search_products'), ('product_details','get_product'), ('cart_creation','create_cart'), ('cart_add','add_to_cart'), ('cart_read','get_cart'), ('cart_update','update_cart_item'), ('cart_replace','replace_cart_items'), ('discount_apply','apply_discount_code'), ('discount_remove','remove_discount_code'), ('fulfillment_options','get_shipping_options'), ('checkout_handoff','create_checkout'), ('checkout_read','get_checkout'), ('checkout_address','set_shipping_address'), ('fulfillment_select','select_shipping_option'), ('checkout_update','update_checkout'), ('checkout_completion','complete_checkout'), ('order_read','get_order'), ('checkout_cancel','cancel_checkout')]
        # Destructive cart checks precede handoff; refill after removal so checkout
        # never silently uses an empty cart. Cancellation gets its own fresh cart.
        if 'remove_from_cart' in ops:
            if 'add_to_cart' not in ops: raise HTTPException(409, 'Removal journey requires cart add')
            index = next((i for i, (_, op) in enumerate(order) if op == 'create_checkout'), len(order))
            order[index:index] = [('cart_remove','remove_from_cart'), ('cart_refill','add_to_cart')]
        if 'cancel_cart' in ops:
            order.extend([('cancel_cart_creation','create_cart'), ('cart_cancel','cancel_cart')])
        report = {'run_id': uid(), 'store_id': store_id, 'actor': actor['id'], 'binding': binding(store_id), 'state': 'running', 'environment': store['environment'], 'native_http': native_http, 'product_id': body.product_id, 'query': body.query, 'discount_code': body.discount_code, 'values': {}, 'created_at': time.time(), 'updated_at': time.time(), 'mode': 'controlled non-payment transaction', 'payment_completed': False, 'path': ['gateway','policy','mapping','job','edge_worker','connector','merchant'], 'stages': [{'name': name, 'operation': op, 'state': 'pending', **({'evidence_only': True} if op in EVIDENCE_ONLY else {})} for name, op in order if op in ops]}
        if direct:
            report.update(bootstrap_sidecar=True, mode='direct', installation_id=direct['id'],
                          verification_expires_at=time.time() + 3600,
                          path=['agent', 'sidecar', 'gateway', 'sidecar', 'private_bridge', 'merchant_business_services'])
        elif native_http:
            report['path'] = ['gateway', 'policy', 'sidecar', 'private_bridge', 'merchant_business_services']
        with dbs.db() as db:
            db.execute('INSERT INTO connection_runs VALUES(?,?,?,?,?,?,?,?)', (report['run_id'], store_id, actor['id'], report['binding'], 'running', encode(report), report['created_at'], report['updated_at']))
        dbs.event(actor['org'], store_id, actor['id'], 'connection_test.started', {'run_id': report['run_id']})
        if background:
            schedule(report)
            return report
        return await drive(report)

    @app.get(root + '/connection-tests')
    def list_runs(store_id: str, actor=Depends(state.user_dependency)):
        state.owned(store_id, actor)
        with dbs.db() as db:
            return {'runs': [json.loads(row['body']) for row in db.execute('SELECT body FROM connection_runs WHERE store=? ORDER BY created DESC LIMIT 20', (store_id,))]}

    @app.get(root + '/connection-tests/{run_id}')
    def read_run(store_id: str, run_id: str, actor=Depends(state.user_dependency)):
        state.owned(store_id, actor)
        return get(store_id, run_id)[1]

    @app.post(root + '/connection-tests/{run_id}/publish-evidence')
    async def publish_evidence(store_id: str, run_id: str, actor=Depends(state.user_dependency)):
        state.owned(store_id, actor)
        row, report = get(store_id, run_id)
        if row['actor'] != actor['id'] or row['binding'] != binding(store_id) or report['state'] != 'passed':
            raise HTTPException(409, 'Current owner and passed configuration-bound run required')
        state.require_agent_access(store_id)
        report['activation'] = {'state': 'active', 'agent_access_enabled': True}
        report['scanner_evidence'] = await state.publish_scanner_evidence(state.store_row(store_id), report)
        persist(report)
        return report

    @app.post(root + '/connection-tests/{run_id}/resume')
    async def resume(store_id: str, run_id: str, background: bool = False, actor=Depends(state.user_dependency)):
        state.owned(store_id, actor)
        row, report = get(store_id, run_id)
        if row['actor'] != actor['id'] or row['binding'] != binding(store_id):
            raise HTTPException(409, 'Operator or configuration changed; do not resume this run')
        with dbs.db() as db:
            changed = db.execute("UPDATE connection_runs SET state='running' WHERE id=? AND store=? AND state='waiting_for_approval'", (run_id, store_id)).rowcount
        if changed != 1:
            raise HTTPException(409, 'Only approval-paused runs can resume. Failed, running or uncertain writes require inspection; no blind retry')
        if background:
            report['state'] = 'running'
            persist(report)
            schedule(report)
            return report
        return await drive(report)
