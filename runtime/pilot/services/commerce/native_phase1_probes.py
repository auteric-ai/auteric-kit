"""Adversarial, non-payment checks for a verified Native HTTP Phase 1 run."""
from fastapi import HTTPException


async def verify(app, report):
    """Return controls only when live Gateway behaviour proves each one.

    The probes reuse the isolated cart created by the operator's connection
    run.  Remote disable is restored in ``finally`` before this function
    returns, even when a probe fails.
    """
    if not report.get('native_http'):
        return {'passed': False, 'reason': 'Native probes do not apply to the legacy worker transport'}
    state, sid = app.state, report['store_id']
    stages = {s['operation']: s for s in report['stages'] if s.get('state') == 'passed'}
    cart_id = report['values'].get('cart_id')
    add = stages.get('add_to_cart')
    required = {'create_cart', 'get_cart', 'add_to_cart'}
    if not cart_id or not required <= set(stages):
        return {'passed': False, 'reason': 'Phase 1 cart probes require successful create, read and add stages'}
    if report.get('bootstrap_sidecar'):
        return await verify_sidecar_bootstrap(app, report, stages, cart_id, add)
    agent = 'operator:' + report['actor']
    session = 'connection-test:' + report['run_id']
    try:
        # Replay the exact previous gateway action: the durable result must be
        # returned without a second merchant mutation.
        replay = await state.execute_action(sid, 'add_to_cart', add['request'], agent, session,
                                            report['run_id'] + ':' + add['name'], operator_id=report['actor'])
        if not replay.get('replayed'):
            return {'passed': False, 'reason': 'Duplicate mutation was not replayed durably'}
        # A distinct pairwise subject must not read the first subject's cart.
        try:
            await state.execute_action(sid, 'get_cart', {'cart_id': cart_id},
                                       'probe:foreign:' + report['run_id'], 'probe-session:' + report['run_id'],
                                       'foreign-cart-read', operator_id=report['actor'])
        except HTTPException as exc:
            if exc.status_code not in {403, 404}:
                return {'passed': False, 'reason': 'Foreign cart probe failed unexpectedly'}
        else:
            return {'passed': False, 'reason': 'Foreign buyer could read the isolated cart'}
        # Disable a read capability only long enough to prove that a new call
        # is stopped at Gateway, then restore its previous effective policy.
        from .installations import set_capability_policy
        set_capability_policy(state.store, sid, 'get_cart', False, 'connection-test:' + report['actor'])
        try:
            try:
                await state.execute_action(sid, 'get_cart', {'cart_id': cart_id}, agent, session,
                                           'disabled-cart-read', operator_id=report['actor'])
            except HTTPException as exc:
                if exc.status_code != 403:
                    return {'passed': False, 'reason': 'Remote-disable probe returned an unexpected status'}
            else:
                return {'passed': False, 'reason': 'Gateway accepted a newly disabled capability'}
        finally:
            set_capability_policy(state.store, sid, 'get_cart', True, 'connection-test:' + report['actor'])
        return {'passed': True}
    except HTTPException as exc:
        return {'passed': False, 'reason': str(exc.detail)[:200]}


async def verify_sidecar_bootstrap(app, report, stages, cart_id, add):
    """Extend the existing probes to the exact-input Sidecar bootstrap route.

    No activation or alternate run state is owned here. A schema-valid BLOCK
    and a real foreign session must both be denied before evidence is promoted.
    """
    import json
    from .storage import uid
    state, sid = app.state, report['store_id']
    call = state.execute_sidecar_verification
    run = report['run_id']
    try:
        replay = await call(report, 'add_to_cart', add['request'], run + ':' + add['name'])
        if replay.get('state') != 'executed' or replay.get('result') != add['result']:
            return {'passed': False, 'reason': 'Exact Sidecar replay failed'}
        policy = json.loads(state.store_row(sid)['policy'])
        quantity = policy['max_quantity_per_item'] + 1
        if quantity > 999:
            return {'passed': False, 'reason': 'Select a schema-valid quantity limit for the BLOCK probe'}
        blocked = await call(report, 'add_to_cart', {**add['request'], 'quantity': quantity}, run + ':policy-block')
        if blocked.get('state') != 'blocked':
            return {'passed': False, 'reason': 'Schema-valid action was not blocked by the existing policy engine'}
        with state.store.db() as db:
            grant = db.execute('SELECT 1 FROM sidecar_grants WHERE action_id=?', (blocked['action_id'],)).fetchone()
        if grant:
            return {'passed': False, 'reason': 'Blocked action received a merchant execution grant'}
        current = await call(report, 'get_cart', {'cart_id': cart_id}, run + ':after-block')
        if current.get('result') != stages['get_cart']['result']:
            return {'passed': False, 'reason': 'Duplicate or blocked mutation changed merchant state'}
        try:
            await call(report, 'get_cart', {'cart_id': cart_id}, run + ':foreign', test_run=uid())
        except HTTPException as exc:
            if exc.status_code not in {403, 404}:
                return {'passed': False, 'reason': 'Foreign cart denial returned an unexpected status'}
        else:
            return {'passed': False, 'reason': 'Foreign session could read the isolated cart'}
        # The ordinary execution entry point must retain its disabled-capability
        # gate even for an owner. Exact bootstrap authority is host-only.
        if not state.operation_enabled(sid, 'get_cart'):
            try:
                await state.execute_action(sid, 'get_cart', {'cart_id': cart_id},
                    'operator:' + report['actor'], 'connection-test:' + run, run + ':disabled',
                    operator_id=report['actor'])
            except HTTPException as exc:
                if exc.status_code != 403:
                    return {'passed': False, 'reason': 'Disabled capability did not fail closed'}
            else:
                return {'passed': False, 'reason': 'Ordinary operator bypassed capability disable'}
        auth_proven = await state.probe_sidecar_verification_auth(report, add['action_id'])
        if not auth_proven:
            return {'passed': False, 'reason': 'Caller authorization and execution replay denial were not proven'}
        return {'passed': True, 'idempotency': True, 'session_ownership': True,
                'policy_block': True, 'unchanged_cart': True,
                # JWT nonce replay requires its own probe, not action replay.
                'replay_protection': True, 'agent_authorization': True,
                'blocked_action_id': blocked['action_id']}
    except HTTPException as exc:
        return {'passed': False, 'reason': str(exc.detail)[:200]}
