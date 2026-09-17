"""Load an installed Store's Storefront credential using its connector identity."""
import re

import httpx

from ..http import request_json
from ..mapping import safe_base_url
from ..manual import ManualConnector
from .shopify import ShopifyConnector


async def installed_connector(*, api_url, store_id, token, database, development=False, transport=None):
    base = safe_base_url(api_url, allow_loopback=development)
    if not re.fullmatch(r'[a-zA-Z0-9_-]{1,200}', store_id):
        raise ValueError('Invalid Store identity')
    async with httpx.AsyncClient(timeout=15, follow_redirects=False, trust_env=False, transport=transport) as client:
        config = await request_json(client, 'GET', base + '/api/commerce/stores/' + store_id + '/shopify/connector-config', headers={'Authorization': 'Bearer ' + token})
    if set(config) != {'shop', 'currency', 'token', 'token_type'}:
        raise ValueError('Unexpected Shopify installation configuration')
    return ManualConnector(ShopifyConnector(**config, database=database))
