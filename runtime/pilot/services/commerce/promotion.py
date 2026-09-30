"""Explicit reviewed promotion. Never execute a production mutation for evidence.

Release digest is declared by the authenticated operator-owned worker, not remote
attestation. Owners must build/hash the actual connector release artifact.
"""
import json
import time

from fastapi import Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from .adapter_manifest import manifest_for
from .storage import encode, uid


class PromotionRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    source_store: str
    source_mapping: str
    expected_fingerprint: str = Field(pattern=r'^[a-f0-9]{64}$')
    release_digest: str = Field(pattern=r'^[a-f0-9]{64}$')
    acknowledge_production_promotion: bool


def attach_promotion(app, dbs, mapping_rows):
    state = app.state
    reads = {'search_products', 'get_product', 'get_cart', 'get_checkout'}

    def source_evidence(source, mapping, release):
        tests = json.loads(mapping['tests'] or 'null')
        if source['environment'] not in {'sandbox', 'staging'} or mapping['state'] != 'active':
            raise HTTPException(409, 'Source must be an active non-production mapping')
        if not tests or tests.get('status') != 'pass' or tests.get('fingerprint') != mapping['fingerprint'] or tests.get('environment') != source['environment'] or tests.get('tested_at', 0) < time.time() - 86400 or tests.get('connector_release') != release:
            raise HTTPException(409, 'Fresh exact non-production contract evidence for this release is required')
        return tests

    def target_evidence(store, release):
        if store['environment'] != 'production':
            raise HTTPException(409, 'Promotion target must be a Production Store')
        if store['connector_release'] != release or not store['heartbeat'] or store['heartbeat'] < time.time() - 40:
            raise HTTPException(409, 'Production worker must report this same release with a current heartbeat')
        for mapping in mapping_rows(store['id']):
            tests = mapping['tests']
            if mapping['operation'] in reads and mapping['state'] == 'active' and tests and tests.get('status') == 'pass' and tests.get('kind') != 'environment_promotion' and tests.get('environment') == 'production' and tests.get('fingerprint') == mapping['fingerprint'] and tests.get('connector_release') == release and tests.get('tested_at', 0) > time.time() - 86400:
                return mapping['id']
        raise HTTPException(409, 'An active passing production read mapping for this release is required')

    def validate(store, mapping, evidence):
        with dbs.db() as db:
            record = db.execute('SELECT * FROM mapping_promotions WHERE id=? AND store=? AND mapping=?', (evidence.get('promotion_id'), store['id'], mapping['id'])).fetchone()
        if not record or json.loads(record['evidence']) != evidence:
            raise HTTPException(409, 'Promotion evidence is not bound to this mapping')
        source = state.store_row(record['source_store'])
        if source['org'] != store['org']:
            raise HTTPException(409, 'Promotion ownership changed')
        source_mapping = state.selected_mapping(source['id'], version=record['source_mapping'])
        if source_mapping['fingerprint'] != mapping['fingerprint']:
            raise HTTPException(409, 'Promoted mapping differs from reviewed source')
        source_evidence(source, source_mapping, record['release_digest'])
        target_evidence(store, record['release_digest'])

    state.validate_promotion = validate

    def runtime_mapping(store, mapping):
        # Native HTTP execution is authorized by the installation's immutable
        # binding plus capability evidence, rather than legacy mapping
        # promotion records. The synthetic descriptor handed to Gateway is
        # intentionally not a promotion candidate.
        from .installations import get_installation
        installation = get_installation(dbs, store['id'], store['environment'])
        if installation and installation['transport'] == 'native_http':
            return
        if store['environment'] != 'production':
            return
        evidence = json.loads(mapping['tests'] or 'null')
        if not evidence or evidence.get('status') != 'pass' or evidence.get('fingerprint') != mapping['fingerprint'] or evidence.get('environment') != 'production':
            raise HTTPException(409, 'Production mapping lacks valid environment evidence')
        if not store['connector_release'] or evidence.get('connector_release') != store['connector_release']:
            raise HTTPException(409, 'Production mapping evidence belongs to a different connector release')
        if mapping['operation'] not in reads and evidence.get('kind') not in {'environment_promotion', 'hosted_release_certification'}:
            raise HTTPException(409, 'Production write mappings require explicit promotion')
        if evidence.get('kind') == 'hosted_release_certification':
            state.validate_hosted_certification(store, mapping, evidence)
        if evidence.get('kind') == 'environment_promotion':
            with dbs.db() as db:
                record = db.execute('SELECT evidence FROM mapping_promotions WHERE id=? AND store=? AND mapping=?', (evidence.get('promotion_id'), store['id'], mapping['id'])).fetchone()
            if not record or json.loads(record['evidence']) != evidence or evidence.get('connector_release') != store['connector_release']:
                raise HTTPException(409, 'Production release differs from promoted evidence')

    state.validate_runtime_mapping = runtime_mapping

    @app.post('/api/commerce/stores/{store_id}/mappings/{version}/promote')
    def promote(store_id: str, version: str, body: PromotionRequest, actor=Depends(state.user_dependency)):
        target = state.owned(store_id, actor)
        source = state.owned(body.source_store, actor)
        mapping = state.selected_mapping(store_id, version=version)
        original = state.selected_mapping(source['id'], version=body.source_mapping)
        if not body.acknowledge_production_promotion or mapping['state'] != 'draft':
            raise HTTPException(409, 'Explicit promotion review of a target draft is required')
        if original['fingerprint'] != mapping['fingerprint'] or mapping['fingerprint'] != body.expected_fingerprint:
            raise HTTPException(409, 'Source, target and reviewed fingerprints must match exactly')
        source_test = source_evidence(source, original, body.release_digest)
        read_mapping = target_evidence(target, body.release_digest)
        promotion_id = uid()
        evidence = {
            'kind': 'environment_promotion', 'promotion_id': promotion_id,
            'status': 'pass', 'environment': 'production', 'fingerprint': mapping['fingerprint'],
            'tested_at': time.time(), 'connector_release': body.release_digest,
            'source_store': source['id'], 'source_mapping': original['id'],
            'source_tested_at': source_test['tested_at'], 'production_read_mapping': read_mapping,
            'reviewer': actor['id'], 'production_mutation_executed': False,
            'production_ready': False,
            'adapter_manifest': manifest_for(mapping['operation'], mapping['fingerprint']),
        }
        with dbs.db() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('INSERT INTO mapping_promotions VALUES(?,?,?,?,?,?,?,?,?)', (promotion_id, store_id, version, source['id'], original['id'], body.release_digest, encode(evidence), actor['id'], time.time()))
            db.execute('UPDATE mappings SET tests=? WHERE id=? AND store=?', (encode(evidence), version, store_id))
        dbs.event(actor['org'], store_id, actor['id'], 'mapping.promoted', evidence)
        return {'evidence': evidence, 'state': 'draft', 'activation_required': True}
