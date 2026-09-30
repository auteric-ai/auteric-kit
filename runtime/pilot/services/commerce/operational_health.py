"""Operational evidence projection; configuration is never a successful request."""
import json
import time


def operational_checks(app, dbs, store, exposed):
    def check(ok, detail, evidence=None):
        return {'state': 'active' if ok else 'needs_action', 'detail': detail, 'evidence': evidence}

    def current_run(sid):
        binding = app.state.connection_binding(sid)
        with dbs.db() as db:
            row = db.execute('SELECT body FROM connection_runs WHERE store=? AND binding=? ORDER BY created DESC LIMIT 1', (sid, binding)).fetchone()
        return json.loads(row['body']) if row else None

    run = current_run(store['id'])
    fresh = bool(run and run['updated_at'] > time.time() - 86400)
    passed = bool(fresh and run['state'] == 'passed')
    successful = {s['operation']: s for s in run['stages'] if s['state'] == 'passed'} if fresh else {}
    covered = set(successful) if passed else set()
    phase1_probes = run.get('phase1_probes') if passed and isinstance(run, dict) else None
    with dbs.db() as db:
        installation = db.execute(
            'SELECT transport FROM installations WHERE store_id=? AND environment=? AND revoked_at IS NULL',
            (store['id'], store['environment']),
        ).fetchone()
    native_runtime = bool(installation and installation['transport'] == 'native_http')
    runtime_safety_ok = not native_runtime or (
        isinstance(phase1_probes, dict) and phase1_probes.get('passed') is True
    )
    runtime_safety_detail = (
        'Runtime replay, idempotency and ownership probes passed'
        if native_runtime and runtime_safety_ok
        else 'Native runtime safety probes require review'
        if native_runtime
        else 'Native runtime safety probes are not required for this transport'
    )
    # Legacy production transports never perform cart writes to earn a green
    # status. A verified Native HTTP runtime is the narrow Phase 1 exception:
    # its controlled test creates an isolated pairwise-owned cart but never
    # completes payment or creates an order.
    inherited = set()
    if store['environment'] == 'production' and passed:
        with dbs.db() as db:
            rows = [dict(r) for r in db.execute("SELECT * FROM mappings WHERE store=? AND state='active'", (store['id'],))]
        for mapping in rows:
            evidence = json.loads(mapping['tests'] or 'null')
            if not evidence or evidence.get('kind') != 'environment_promotion': continue
            try:
                app.state.validate_runtime_mapping(store, mapping)
                source = app.state.store_row(evidence['source_store'])
                source_run = current_run(source['id'])
                source_mapping = app.state.selected_mapping(source['id'], operation=mapping['operation'])
                if source['org'] != store['org'] or source['policy'] != store['policy'] or source['connector_release'] != store['connector_release'] or source_mapping['id'] != evidence['source_mapping']: continue
                if source_run and source_run['state'] == 'passed' and source_run['updated_at'] > time.time() - 86400 and any(s['operation'] == mapping['operation'] and s['state'] == 'passed' for s in source_run['stages']):
                    covered.add(mapping['operation']); inherited.add(mapping['operation'])
            except Exception:
                continue
    evidence = {'run_id': run['run_id'], 'state': run['state'], 'updated_at': run['updated_at']} if run else None
    with dbs.db() as db:
        uncertain = db.execute("SELECT count(*) FROM jobs WHERE store=? AND (state='uncertain' OR (state='claimed' AND expires<?)) AND operation NOT IN ('search_products','get_product','get_cart','get_checkout')", (store['id'], time.time())).fetchone()[0]
        locked = db.execute('SELECT count(*) FROM resource_locks WHERE store=?', (store['id'],)).fetchone()[0]
    checks = {
        'critical_errors': check(uncertain == 0 and locked == 0, 'No unresolved ambiguous writes' if not uncertain and not locked else 'Resolve ambiguous or in-flight actions before activation', {'uncertain_writes': uncertain, 'locked_resources': locked}),
        'merchant_reachability': check(bool(successful), 'Confirmed merchant receipts in a recent current-configuration run' if successful else 'Run a Connection Test against the current connector and mappings', evidence),
        'gateway': check(passed, 'Current-configuration Gateway journey passed' if passed else 'No current successful Gateway journey', evidence),
        'policy_enforcement': check(passed and all(s.get('policy_decision') and s['policy_decision'].get('outcome') in {'ALLOW', 'REQUIRE_APPROVAL'} for s in successful.values()), 'Recorded policy decisions from actual Gateway steps', evidence),
        'connection_test': check(bool(exposed) and passed and exposed <= covered, 'Every enabled operation has current passing evidence' if passed and exposed <= covered else 'Enabled operations still require current passing connection evidence', {'run': evidence, 'uncovered_operations': sorted(exposed-covered), 'promoted_staging_operations': sorted(inherited)}),
        'runtime_safety': check(runtime_safety_ok, runtime_safety_detail, phase1_probes if native_runtime else None),
    }
    names = {'search_products': 'product_search', 'get_product': 'product_lookup', 'create_cart': 'cart_creation', 'get_cart': 'cart_read', 'add_to_cart': 'cart_mutation', 'create_checkout': 'checkout_handoff', 'get_checkout': 'checkout_read'}
    for operation in sorted(exposed):
        checks[names.get(operation, operation)] = check(operation in covered, 'Passing promoted staging evidence; not a live production write' if operation in inherited else 'Current successful Gateway step' if operation in covered else 'This enabled action has no current passing connection evidence', successful.get(operation, evidence))
    return checks
