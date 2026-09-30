"""Optional integration with the pinned anthropics/commerce-agents MerchantBackend.

Import only after installing the upstream commerce-common and merchant-agent-core.
No Anthropic source is copied or patched here.
"""
import uuid
from datetime import datetime, timezone
from urllib.parse import quote

from merchant_agent.backend import MerchantBackend
from merchant_agent.changes import ChangeNotApplicable
from merchant_agent.types import ListingDetails, PricingContext, StagedChange


class AutericMerchantBackend(MerchantBackend):
    def __init__(self, client, merchant_id):
        self.client = client
        self.merchant_id = merchant_id

    async def _scope(self, session):
        if session.merchant_id != self.merchant_id:
            raise PermissionError("Session merchant does not match this gateway binding")
        capabilities = await self.client.request("GET", "v1/capabilities")
        if capabilities["merchant_id"] != self.merchant_id:
            raise PermissionError("Authenticated gateway is bound to another merchant")

    @staticmethod
    def _change(row):
        if row['state'] in {'applying', 'uncertain', 'stale'}:
            raise RuntimeError(f"Auteric change is {row['state']}; operator review required")
        body = row['proposal']
        return StagedChange(change_id=row['id'], kind='price_update',
                            status='applied' if row['state']=='applied' else 'discarded' if row['state']=='discarded' else 'staged',
                            summary=f"Price change for {body['variant']['sku']}",
                            items=[{'target':body['variant']['id'],'field':'price','before':body['variant']['price'],'after':body['new_price']}],
                            created_at=datetime.fromtimestamp(body['created_at'], timezone.utc),
                            created_by=row['created_by'], created_by_kind='agent', currency=body['variant']['currency'],
                            applied_by=row['approved_by'] if row['state']=='applied' else None,
                            guardrail_notes=['Separate gateway approval is required. Chat approval is not sufficient.', f"Mode: {body['mode']}"])

    async def get_listing(self, session, listing_id):
        await self._scope(session)
        row = await self.client.request('GET', 'v1/variant', params={'id':listing_id})
        return ListingDetails(listing_id=row['id'], title=row['title'], price=float(row['price']), currency=row['currency'], stock=row['stock'],
                              attributes={'sku':row['sku']}, status='active' if row['stock']>0 else 'out_of_stock')

    async def get_pricing_context(self, session, listing_id):
        listing = await self.get_listing(session, listing_id)
        return PricingContext(listing_id=listing.listing_id, current_price=listing.price, currency=listing.currency)

    async def stage_price_update(self, session, items, note=None):
        await self._scope(session)
        if len(items) != 1:
            raise ChangeNotApplicable('This adapter stages one variant per approval; bulk writes are unsupported')
        row = await self.client.request('POST','v1/changes',json={'variant_id':items[0].listing_id,'new_price':str(items[0].new_price),
                                                               'reason':note or 'Merchant agent price proposal','idempotency_key':uuid.uuid4().hex})
        return self._change(row)

    async def get_pending_changes(self, session):
        await self._scope(session)
        rows = await self.client.request('GET','v1/changes')
        return [self._change(row) for row in rows if row['state'] in {'staged','approved'}]

    async def apply_change(self, session, change_id):
        await self._scope(session)
        # A forged approval mark in the host/LLM cannot bypass gateway approval.
        return self._change(await self.client.request('POST',f'v1/changes/{quote(change_id, safe="")}/execute'))

    async def discard_change(self, session, change_id, actor_kind=None):
        raise ChangeNotApplicable('Discard requires the separate operator credential via the gateway API')

    async def get_merchant_context(self, session):
        await self._scope(session)
        return {'limitations':[{'source':'auteric-v0.1','note':'Only exact variant reads and separately approved single-variant price changes are connected. No analytics, campaigns or checkout.'}]}

    async def search_listings(self, session, query, filters=None, limit=8):
        raise ChangeNotApplicable('Catalog search is not connected; provide an exact variant ID')

    async def get_business_snapshot(self, session, period=None):
        raise ChangeNotApplicable('Business analytics is not connected')

    async def query_metrics(self, session, metric, period=None, granularity='day', segment=None):
        raise ChangeNotApplicable('Metrics are not connected')

    async def get_campaign_performance(self, session, campaign_id=None):
        raise ChangeNotApplicable('Campaigns are not connected')

    async def get_inventory_alerts(self, session):
        raise ChangeNotApplicable('Inventory alerts are not connected')

    async def get_order_issues(self, session):
        raise ChangeNotApplicable('Orders are not connected')

    async def stage_listing_update(self, session, listing_id, fields, note=None):
        raise ChangeNotApplicable('Listing writes are not connected')

    async def stage_inventory_action(self, session, items, note=None):
        raise ChangeNotApplicable('Inventory writes are not connected')

    async def stage_promotion(self, session, promotion):
        raise ChangeNotApplicable('Scheduled promotions are not connected; use a single-variant price proposal')

    async def stage_campaign(self, session, campaign):
        raise ChangeNotApplicable('Campaign writes are not connected')
