from urllib.parse import urlparse

import httpx


class GatewayClient:
    def __init__(self, url, token, *, transport=None):
        parsed = urlparse(url)
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("Credentials, queries and fragments do not belong in gateway URLs")
        if parsed.scheme != "https" and not (parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "localhost", "::1"}):
            raise ValueError("Remote gateway requires HTTPS")
        self.http = httpx.AsyncClient(base_url=url.rstrip('/')+'/', headers={"Authorization": f"Bearer {token}"},
                                      timeout=40, follow_redirects=False, transport=transport)

    async def request(self, method, path, **kwargs):
        response = await self.http.request(method, path, **kwargs)
        if response.status_code >= 300:
            # Do not put headers/credentials or provider bodies into model-visible exceptions.
            raise RuntimeError(f"Auteric gateway refused operation (HTTP {response.status_code}); review approval and policy in the operator console")
        return response.json()

    async def close(self):
        await self.http.aclose()
