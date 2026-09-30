"""Domain claims. The secret owner token is distinct from the published challenge."""
import hashlib
import secrets
import sqlite3
import time

import httpx
from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from adapters.generic_web import safe_get
from public_reports import domain_name
from public_pages import esc, render_page


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


class MerchantClaims:
    def __init__(self, path):
        self.path = path
        with sqlite3.connect(path) as c:
            c.executescript('''
            CREATE TABLE IF NOT EXISTS merchant_claims(
              domain TEXT PRIMARY KEY, owner_hash TEXT NOT NULL, verified REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS claim_challenges(
              id TEXT PRIMARY KEY, domain TEXT NOT NULL, owner_hash TEXT NOT NULL,
              challenge TEXT NOT NULL, expires REAL NOT NULL);
            ''')

    def status(self, domain):
        with sqlite3.connect(self.path) as c:
            row = c.execute('SELECT verified FROM merchant_claims WHERE domain=?', (domain,)).fetchone()
        # Connected/protected require commerce runtime evidence, never a public signature.
        return 'claimed' if row else 'unclaimed'

    def start(self, domain):
        token, challenge, claim_id = secrets.token_urlsafe(32), secrets.token_urlsafe(32), secrets.token_hex(16)
        with sqlite3.connect(self.path) as c:
            c.execute('DELETE FROM claim_challenges WHERE expires<?', (time.time(),))
            if c.execute('SELECT count(*) FROM claim_challenges WHERE domain=?', (domain,)).fetchone()[0] >= 5:
                raise HTTPException(429, 'Too many active challenges for this domain; try again later')
            if self.status(domain) != 'unclaimed':
                raise HTTPException(409, 'This domain is already claimed')
            c.execute('INSERT INTO claim_challenges VALUES(?,?,?,?,?)',
                      (claim_id, domain, digest(token), challenge, time.time() + 3600))
        return {'claim_id': claim_id, 'owner_token': token, 'challenge': challenge,
                'verification_url': f'https://{domain}/.well-known/auteric-claim.txt', 'expires_in': 3600}

    def challenge(self, claim_id, token):
        with sqlite3.connect(self.path) as c:
            c.row_factory = sqlite3.Row
            row = c.execute('SELECT * FROM claim_challenges WHERE id=? AND expires>?', (claim_id, time.time())).fetchone()
        if not row or not secrets.compare_digest(row['owner_hash'], digest(token)):
            raise HTTPException(403, 'Invalid or expired claim credentials')
        return dict(row)

    def finish(self, claim_id, token, observed):
        row = self.challenge(claim_id, token)
        if not secrets.compare_digest(observed.strip(), row['challenge']):
            raise HTTPException(409, 'The ownership file does not match the challenge')
        with sqlite3.connect(self.path) as c:
            try:
                c.execute('INSERT INTO merchant_claims VALUES(?,?,?)', (row['domain'], row['owner_hash'], time.time()))
            except sqlite3.IntegrityError:
                raise HTTPException(409, 'This domain is already claimed') from None
            c.execute('DELETE FROM claim_challenges WHERE domain=?', (row['domain'],))
        return {'domain': row['domain'], 'status': 'claimed'}


def install_claim_routes(app, claims, base_url, shell=None):
    @app.middleware('http')
    async def private_claim_headers(request, call_next):
        response = await call_next(request)
        if request.url.path.startswith(('/claim/', '/api/', '/internal/', '/openapi.json')):
            response.headers['Cache-Control'] = 'no-store'
            response.headers['X-Robots-Tag'] = 'noindex, nofollow'
            response.headers['Referrer-Policy'] = 'no-referrer'
        return response

    @app.post('/api/claims/{domain}')
    def begin(domain: str):
        try:
            domain = domain_name(domain)
        except ValueError:
            raise HTTPException(422, 'A valid public domain is required') from None
        return JSONResponse(claims.start(domain))

    @app.post('/api/claims/{claim_id}/verify')
    async def verify(claim_id: str, request: Request):
        token = request.headers.get('Authorization', '').removeprefix('Bearer ')
        row = claims.challenge(claim_id, token)
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                result = await safe_get(client, f"https://{row['domain']}/.well-known/auteric-claim.txt", follow_redirects=False)
            if result.status_code != 200:
                raise HTTPException(409, 'Ownership file must return HTTP 200 on this exact HTTPS domain')
        except (httpx.HTTPError, ValueError):
            raise HTTPException(409, 'Could not safely retrieve the ownership file') from None
        return claims.finish(claim_id, token, result.text)

    @app.get('/claim/{domain}')
    def claim_page(domain: str, request: Request):
        try:
            domain = domain_name(domain)
        except ValueError:
            raise HTTPException(404, 'Invalid domain') from None
        body = f'''<section id="publicReport" class="seo-page merchant-report claim-report" data-domain="{esc(domain)}"><div id="claimFlow" data-domain="{esc(domain)}"><p class="eyebrow">AUTERIC · STORE CONNECTION</p><h1>Bring {esc(domain)} to AI shopping</h1>
        <p class="merchant-lede">Start with the business steps: decide what customers can discover and which actions agents may take. You do not need to upload a file or change your website to begin.</p>
        <div class="merchant-steps claim-steps"><article><span>1</span><h2>Review your opportunity</h2><p>See the catalog, shopping and protection gaps we found.</p></article><article><span>2</span><h2>Choose what to connect</h2><p>Start with catalog visibility, then add cart and checkout only when you are ready.</p></article><article><span>3</span><h2>Verify when needed</h2><p>Auteric asks for proof only before it marks the business as claimed or enables live actions.</p></article></div>
        <a class="btn primary" href="/onboarding/{esc(domain)}">Start my store plan</a>
        <details class="merchant-advanced"><summary>I manage the website and want to verify ownership now</summary>
        <p>Website-file verification is an optional technical route for a webmaster or agency. It prevents someone else from claiming your public store. It is not required to view your plan.</p>
        <button id="beginClaim" class="btn ghost">Create website verification file</button>
        <div id="claimInstructions" hidden><p>Ask the person who manages your website to publish this one-time text at <strong id="claimUrl"></strong></p>
        <pre id="claimText"></pre><p>The code expires in one hour. It is used only to verify control of this domain.</p>
        <button id="verifyClaim" class="btn primary">Verify website ownership</button></div>
        <p id="claimMessage" role="status"></p></details><div id="claimNext" hidden>
        <h2>Website verified</h2><p>Now continue to the connection plan for your store.</p>
        <a class="btn primary" href="/onboarding/{esc(domain)}">Continue to my store plan</a></div></section>
        <script src="/static/claims.js" defer></script>'''
        if shell:
            rendered = shell(request, title=f'Claim {domain} | Auteric',
                             description='Verify store ownership to connect and improve AI commerce readiness.',
                             path='/claim/' + domain, noindex=True)
            markup = rendered.body.decode()
            markup = markup.replace('<main id="main-content">', '<main id="main-content">' + body)
            markup = markup.replace('class="landing" id="landingView"', 'class="landing hidden" id="landingView"')
            return HTMLResponse(markup)
        return HTMLResponse(render_page('/claim/' + domain, 'Claim ' + domain,
                            'Claim → Connect → Improve → Protect', body, base_url(request), index=False))
