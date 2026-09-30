"""Authenticated integration boundary for measured, domain-bound runtime results.

Only Auteric's trusted backend may submit receipts. Public manifests, browser
claims and connector selection cannot promote a merchant's runtime status.
"""
import copy
import hashlib
import json
import os
import secrets
import sqlite3
import time
from datetime import datetime, timezone
from typing import Literal

from fastapi import Header, HTTPException
from pydantic import BaseModel, Field
from public_reports import domain_name
from readiness import ACTIONS, VERSION as READINESS_VERSION

CONTROLS = ('agent_authorization', 'merchant_policy', 'user_intent', 'action_validation',
            'approval_workflow', 'cart_integrity', 'replay_protection', 'idempotency',
            'session_ownership', 'audit_trail', 'protected_checkout')
NATIVE_PHASE1_CONTROLS = ('agent_authorization', 'merchant_policy', 'user_intent',
                          'action_validation', 'cart_integrity', 'replay_protection',
                          'idempotency', 'session_ownership', 'audit_trail')
Control = Literal['agent_authorization', 'merchant_policy', 'user_intent', 'action_validation',
                  'approval_workflow', 'cart_integrity', 'replay_protection', 'idempotency',
                  'session_ownership', 'audit_trail', 'protected_checkout']
Action = Literal['Discover products', 'Read product details', 'Read variants', 'Read availability',
                 'Search products', 'Create cart', 'Add item', 'Change quantity', 'Remove item',
                 'Select variant', 'Get shipping options', 'Checkout handoff', 'Complete payment']


class RuntimeReceipt(BaseModel):
    receipt_id: str = Field(min_length=8, max_length=128, pattern=r'^[a-zA-Z0-9_-]+$')
    connection_id: str = Field(min_length=1, max_length=128)
    observed_at: float
    expires_at: float
    actions: list[Action] = Field(default_factory=list, max_length=13)
    controls: dict[Control, Literal['pass', 'fail', 'unknown']] = Field(default_factory=dict)
    # Only Scanner's authenticated Auteric control-plane ingestion may submit
    # this source.  It lets a verified Auteric domain avoid a second, unrelated
    # Scanner browser claim while preserving the service-key trust boundary.
    source: Literal['auteric_control'] = 'auteric_control'
    scope: Literal['native_phase1', 'full'] = 'full'
    installation_id: str | None = Field(default=None,max_length=200)
    configuration_digest: str | None = Field(default=None,max_length=200)
    run_id: str | None = Field(default=None,max_length=200)
    operations: list[str] = Field(default_factory=list,max_length=20)
    enforcement_path: list[str] = Field(default_factory=list,max_length=10)


class RuntimeReadiness:
    def __init__(self, path):
        self.path = path
        with sqlite3.connect(path) as c:
            c.execute('''CREATE TABLE IF NOT EXISTS runtime_readiness(
                receipt_id TEXT PRIMARY KEY, domain TEXT NOT NULL, observed REAL NOT NULL,
                expires REAL NOT NULL, payload TEXT NOT NULL)''')

    def latest(self, domain):
        with sqlite3.connect(self.path) as c:
            row = c.execute('SELECT payload,expires FROM runtime_readiness WHERE domain=? ORDER BY observed DESC LIMIT 1', (domain,)).fetchone()
        # A newer revocation/expired receipt must not revive an older one.
        return json.loads(row[0]) if row and row[1] > time.time() else None

    def status(self, domain, claimed):
        # A current receipt from the authenticated Control Plane is itself
        # domain-ownership evidence.  Unauthenticated/public callers cannot
        # create such a receipt; ordinary Scanner-connected runtimes still need
        # an explicit Scanner claim.
        r = self.latest(domain)
        if r and not (claimed or r.get('source') == 'auteric_control'):
            r = None
        if not r:
            return 'claimed' if claimed else 'unclaimed'
        required = NATIVE_PHASE1_CONTROLS if r.get('scope') == 'native_phase1' else CONTROLS
        return 'protected' if all(r['controls'].get(k) == 'pass' for k in required) else 'connected'

    def save(self, domain, receipt):
        payload = receipt.model_dump()
        now = time.time()
        if not now - 86400 <= receipt.observed_at <= now + 60 or not receipt.observed_at < receipt.expires_at <= receipt.observed_at + 86400:
            raise HTTPException(422, 'Runtime evidence must be recent and valid for no more than 24 hours')
        encoded = json.dumps(payload, sort_keys=True)
        with sqlite3.connect(self.path) as c:
            c.execute('BEGIN IMMEDIATE')
            old = c.execute('SELECT domain,payload FROM runtime_readiness WHERE receipt_id=?', (receipt.receipt_id,)).fetchone()
            if old and old != (domain, encoded):
                raise HTTPException(409, 'Receipt ID already belongs to different evidence')
            newest = c.execute('SELECT observed FROM runtime_readiness WHERE domain=? ORDER BY observed DESC LIMIT 1', (domain,)).fetchone()
            if not old and newest and receipt.observed_at <= newest[0]:
                raise HTTPException(409, 'Runtime evidence must be newer than the previous receipt')
            c.execute('INSERT OR IGNORE INTO runtime_readiness VALUES(?,?,?,?,?)',
                      (receipt.receipt_id, domain, receipt.observed_at, receipt.expires_at, encoded))
        return payload

    def apply(self, row, receipt=None):
        result = copy.deepcopy(row)
        receipt = receipt if receipt is not None else self.latest(row['domain'])
        r = copy.deepcopy(result.get('public_readiness') or result.get('readiness'))
        result['readiness'] = r
        if not r:
            return result
        if receipt:
            r['runtime_operations']=receipt.get('operations',[])
            r['runtime_scope']=receipt.get('scope','full')
            for action in receipt['actions']:
                r['actions'][action] = 'Observed'
            r['functional'] = round(100 * sum(v == 'Observed' for v in r['actions'].values()) / len(ACTIONS))
            tested = sum(v in {'pass', 'fail'} for v in receipt['controls'].values())
            r['protected'] = round(100 * sum(v == 'pass' for v in receipt['controls'].values()) / len(CONTROLS)) if tested else None
            r['protection_status'] = 'Measured runtime controls' if tested else 'Runtime protection unverified'
            r['runtime_observed_at'] = datetime.fromtimestamp(receipt['observed_at'], timezone.utc).isoformat()
            r['runtime_expires_at'] = datetime.fromtimestamp(receipt['expires_at'], timezone.utc).isoformat()
            r['runtime_controls_tested'] = tested
            r['recommendations'][1] = 'Review remaining unverified actions and revalidate connected commerce capabilities.'
            if all(receipt['controls'].get(k) == 'pass' for k in CONTROLS):
                r['recommendations'][2] = 'Keep Auteric policies current and renew runtime verification before its evidence expires.'
        elif r.get('runtime_observed_at'):
            # Historical snapshots remain immutable, but stale evidence is not current proof.
            r['protected'] = None
            r['protection_status'] = 'Runtime evidence expired; revalidation required'
        return result


def install_runtime_routes(app, runtime, claims, reports):
    @app.post('/internal/readiness/{domain}')
    def receive(domain: str, receipt: RuntimeReceipt, x_scanner_service_key: str | None = Header(default=None)):
        expected = os.getenv('SCANNER_SERVICE_KEY', '')
        if not expected:
            raise HTTPException(503, 'Trusted runtime ingestion is not configured')
        if not secrets.compare_digest(expected, x_scanner_service_key or ''):
            raise HTTPException(401, 'Service authentication required')
        try:
            domain = domain_name(domain)
        except ValueError:
            raise HTTPException(422, 'Invalid public domain') from None
        # The service key authenticates the Auteric Control Plane.  Its receipt
        # is accepted as ownership evidence for the exact domain only; it does
        # not weaken the public claim route for any other ingestion path.
        if claims.status(domain) == 'unclaimed' and receipt.source != 'auteric_control':
            raise HTTPException(409, 'Verify domain ownership first')
        rows = reports.rows(domain)
        if not rows or not rows[0].get('readiness'):
            raise HTTPException(409, f'A {READINESS_VERSION} scan is required first')
        runtime.save(domain, receipt)
        source = copy.deepcopy(rows[0])
        source['public_readiness'] = source.get('public_readiness') or copy.deepcopy(source['readiness'])
        source['source_scan_id'] = source.get('source_scan_id') or source['scan_id']
        source['source_scan_completed_at'] = source.get('source_scan_completed_at') or source['completed_at']
        row = runtime.apply(source, receipt.model_dump())
        row['scan_id'] = 'runtime-' + hashlib.sha256(receipt.receipt_id.encode()).hexdigest()
        row['completed_at'] = datetime.fromtimestamp(receipt.observed_at, timezone.utc).isoformat()
        row['observation_type'] = 'runtime_validation'
        # Public snapshot contains aggregate scores, never receipt IDs or control details.
        if not any(r['scan_id'] == row['scan_id'] for r in reports.rows(domain, history=True)):
            reports.publish_projection(row)
        return {'status': runtime.status(domain, True), 'domain': domain}
