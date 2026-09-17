"""Review-only publication files. No DNS, deployment or merchant-file mutation."""
import json
from urllib.parse import urlsplit


def publication_proposal(framework, upstream):
    parsed = urlsplit(upstream)
    if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment or any(ord(c) < 32 for c in upstream):
        raise ValueError('Publication requires a fixed credential-free HTTPS profile URL')
    if not parsed.path.endswith('/.well-known/ucp'):
        raise ValueError('Upstream must be the generated Store discovery endpoint')
    js = '''const upstream = UPSTREAM;
async function profile() {
  const response = await fetch(upstream, {redirect: 'error', cache: 'no-store', signal: AbortSignal.timeout(5000)});
  if (!response.ok || !response.body) throw new Error('Profile unavailable');
  const reader = response.body.getReader(); const chunks = []; let size = 0;
  try { while (true) { const {done,value} = await reader.read(); if (done) break;
    size += value.byteLength; if (size > 1000000) throw new Error('Profile too large'); chunks.push(value);
  } } finally { await reader.cancel(); }
  const bytes = new Uint8Array(size); let offset = 0;
  for (const chunk of chunks) { bytes.set(chunk, offset); offset += chunk.byteLength; }
  const value = JSON.parse(new TextDecoder().decode(bytes));
  if (!value.ucp || !value.auteric_domain_verification) throw new Error('Invalid profile');
  return JSON.stringify(value);
}
'''.replace('UPSTREAM', json.dumps(upstream))
    py = '''import json
import httpx

UPSTREAM = URL_LITERAL

async def _profile():
    async with httpx.AsyncClient(timeout=5, follow_redirects=False, trust_env=False) as client:
        async with client.stream("GET", UPSTREAM) as response:
            response.raise_for_status()
            if response.status_code != 200:
                raise ValueError("Unexpected profile status")
            data = bytearray()
            async for chunk in response.aiter_bytes():
                data.extend(chunk)
                if len(data) > 1000000:
                    raise ValueError("Profile too large")
    result = json.loads(data)
    if not isinstance(result, dict) or not result.get("ucp") or not result.get("auteric_domain_verification"):
        raise ValueError("Invalid profile")
    return result
'''.replace('URL_LITERAL', repr(upstream))
    mount = None
    if framework == 'next':
        filename = 'app/.well-known/ucp/route.ts'
        content = js + '''export const dynamic = 'force-dynamic';
export async function GET() {
  try { return new Response(await profile(), {headers: {'Content-Type':'application/json', 'Cache-Control':'no-store'}}); }
  catch { return new Response('Profile unavailable', {status:502, headers:{'Cache-Control':'no-store'}}); }
}
'''
        mount = 'Review app/ versus src/app/ layout. Merge this GET route; never overwrite an existing route.'
    elif framework == 'express':
        filename = 'auteric-publication.mjs'
        content = "import { Router } from 'express';\n" + js + '''export const autericPublication = Router();
autericPublication.get('/.well-known/ucp', async (_request, response) => {
  response.set('Cache-Control','no-store');
  try { response.type('application/json').send(await profile()); }
  catch { response.status(502).send('Profile unavailable'); }
});
'''
        mount = 'Import autericPublication and add app.use(autericPublication) before any SPA catch-all. Requires Node with fetch and AbortSignal.timeout.'
    elif framework == 'fastapi':
        filename = 'auteric_publication.py'
        content = py + '''
from fastapi import APIRouter
from fastapi.responses import JSONResponse
router = APIRouter()

@router.get("/.well-known/ucp")
async def discovery():
    try:
        return JSONResponse(await _profile(), headers={"Cache-Control":"no-store"})
    except Exception:
        return JSONResponse({"error":"Profile unavailable"}, status_code=502, headers={"Cache-Control":"no-store"})
'''
        mount = 'Review/install httpx and add app.include_router(router) from this module before catch-all routes.'
    elif framework == 'flask':
        filename = 'auteric_publication.py'
        content = py + '''
import asyncio
from flask import Blueprint, jsonify
blueprint = Blueprint("auteric_publication", __name__)

@blueprint.get("/.well-known/ucp")
def discovery():
    try:
        response = jsonify(asyncio.run(_profile()))
    except Exception:
        response = jsonify({"error":"Profile unavailable"})
        response.status_code = 502
    response.headers["Cache-Control"] = "no-store"
    return response
'''
        mount = 'Review/install httpx and app.register_blueprint(blueprint) in the synchronous Flask application factory.'
    elif framework == 'vite':
        return {'framework': framework, 'files': [], 'review_required': True, 'deployment_required': True, 'instructions': ['Vite builds a static frontend and cannot host a production server route.', 'If the merchant already has Express/FastAPI/Flask, generate that route and route only /.well-known/ucp to it at the hosting layer.', 'Otherwise publish the exact generated JSON as public/.well-known/ucp, configure application/json and no-store at your host, rebuild and deploy; regenerate whenever capabilities change.', 'Do not use a Vite dev-server proxy as production publication.'], 'upstream': upstream, 'automatic_dns_changes': False}
    else:
        raise ValueError('Choose next, express, fastapi, flask or vite')
    return {'framework': framework, 'files': [{'path': filename, 'content': content}], 'review_required': True, 'deployment_required': True, 'instructions': [mount, 'Serve only /.well-known/ucp. Do not proxy human storefront or arbitrary user URLs.', 'Review the diff and tests, deploy explicitly, then run Verify in the Store console.'], 'upstream': upstream, 'automatic_dns_changes': False}
