"""Optional, non-forking wrapper around Anthropic's existing MerchantBackend.

The wrapped backend is trusted host code. This cooperative in-process integration
cannot sandbox a malicious backend or a process that still has merchant credentials.
Only the price.update mutation is connected; all other writes fail closed.
"""
from __future__ import annotations

import hashlib
import json
import logging
import uuid
from datetime import datetime, timezone
from decimal import Decimal

from merchant_agent.backend import MerchantBackend
from merchant_agent.changes import ChangeNotApplicable
from merchant_agent.types import PriceUpdateItem, StagedChange

from .actions import ActionPrincipal, AgentIdentity, CommerceAction, Delegation
from .domain import DomainError, Variant

logger = logging.getLogger("auteric.adapter")


class _MerchantBridge:
    """Translate runtime connector calls into the original backend contract."""

    def __init__(self, backend, session):
        self.backend = backend
        self.session = session.model_copy(deep=True)

    async def get(self, variant_id):
        listing = await self.backend.get_listing(self.session, variant_id)
        pricing = await self.backend.get_pricing_context(self.session, variant_id)
        if listing is None or pricing is None or listing.listing_id != variant_id or pricing.listing_id != variant_id:
            raise DomainError("Merchant did not return the requested price resource", 422)
        if listing.has_options or listing.variants:
            raise DomainError("A family is not an executable price target; select one variant", 422)
        if "stock" not in listing.model_fields_set:
            raise DomainError("Merchant must explicitly supply inventory; default zero is not evidence", 422)
        if Decimal(str(listing.price)) != Decimal(str(pricing.current_price)) or listing.currency != pricing.currency:
            raise DomainError("Merchant listing and pricing context disagree", 422)
        return Variant(id=variant_id, product_id=listing.variant_of or variant_id, title=listing.title,
                       sku=listing.attributes.get("sku", ""), price=Decimal(str(pricing.current_price)),
                       currency=listing.currency, stock=listing.stock)

    async def set_price(self, variant, price):
        # Stage only after runtime authorization. Never trust a staged change ID
        # obtained directly from the model or the raw merchant backend.
        staged = await self.backend.stage_price_update(
            self.session, [PriceUpdateItem(listing_id=variant.id, new_price=float(price))],
            note="Exact price action authorized by Auteric")
        if (staged.kind != "price_update" or staged.status != "staged" or staged.currency != variant.currency
                or len(staged.items) != 1):
            raise DomainError("Merchant staged an unexpected mutation")
        item = staged.items[0]
        if (item.target != variant.id or item.field != "price"
                or Decimal(str(item.before)) != variant.price or Decimal(str(item.after)) != price):
            raise DomainError("Merchant staged payload differs from the authorized action")
        # Narrow the stage-to-apply race; a conditional write in the merchant
        # remains necessary for strict atomic compare-and-set semantics.
        current = await self.get(variant.id)
        if current.model_dump() != variant.model_dump():
            raise DomainError("Merchant state changed while staging; re-evaluate")
        applied = await self.backend.apply_change(self.session, staged.change_id)
        if applied.change_id != staged.change_id or applied.status != "applied":
            raise DomainError("Merchant did not confirm the authorized execution")


class AutericProtectedBackend(MerchantBackend):
    """Host-created backend wrapper. Identity must come from authenticated host code.

    ``backend_id`` is a trusted stable deployment identifier. Omit it for an
    ephemeral wrapper whose pending actions fail closed after restart. Keep the
    wrapper for the session; don't instantiate it per model tool call.
    """

    def __init__(self, backend, *, runtime, principal=None, agent=None, delegation=None, backend_id=None):
        if not isinstance(backend, MerchantBackend):
            raise TypeError("Expected the pinned upstream MerchantBackend interface")
        self._backend, self._runtime = backend, runtime
        self._principal = ActionPrincipal.model_validate(principal or {}).model_copy(deep=True)
        self._agent = AgentIdentity.model_validate(agent or {}).model_copy(deep=True)
        self._delegation = None if delegation is None else Delegation.model_validate(delegation).model_copy(deep=True)
        self._backend_id = backend_id or str(uuid.uuid4())
        self._proposals = {}

    def _scope(self, session):
        if session.merchant_id != self._runtime.merchant_id:
            raise PermissionError("Session merchant does not match the protected runtime")
        if self._principal.subject is not None and session.operator != self._principal.subject:
            raise PermissionError("Session operator differs from the authenticated principal")
        return {"adapter": "anthropic-merchant-backend", "backend": self._backend_id,
                "session_id": session.session_id, "merchant_id": session.merchant_id,
                "principal": self._principal.model_dump(mode="json"), "agent": self._agent.model_dump(mode="json"),
                "delegation": self._delegation.model_dump(mode="json") if self._delegation else None}

    def _bound_runtime(self, session):
        return self._runtime.with_backend(_MerchantBridge(self._backend, session), self._scope(session))

    def _owned(self, session, row):
        action = row["action"]
        if action.get("metadata", {}).get("protected_binding") != self._scope(session):
            raise PermissionError("Action belongs to another protected backend/session")
        if action["principal"] != self._principal.model_dump(mode="json") or action["agent"] != self._agent.model_dump(mode="json"):
            raise PermissionError("Action identity does not match this wrapper")

    @staticmethod
    def _change(row):
        action = row["action"]
        item = action["items"][0]
        state = row["state"]
        if state in {"blocked", "stale", "uncertain", "expired", "executing"}:
            reasons = "; ".join(row["decision"].get("reasons", []))
            raise ChangeNotApplicable(f"Auteric {state}: {reasons}; action_id={row['id']}")
        return StagedChange(change_id=row["id"], kind="price_update",
                            status="applied" if state == "executed" else "discarded" if state == "rejected" else "staged",
                            summary=f"Price change for {item.get('sku') or item['resource_id']}"[:200],
                            items=[{"target": item["resource_id"], "field": "price", "before": item["before"], "after": item["proposed_after"]}],
                            created_at=datetime.fromtimestamp(row["created_at"], timezone.utc),
                            created_by=row["created_by"], created_by_kind="agent", currency=item["currency"],
                            applied_by=(row["approved_by"] or row["created_by"]) if state == "executed" else None,
                            guardrail_notes=[f"Auteric: {state}", *row["decision"].get("reasons", []),
                                             "Upstream host approval gates remain enabled; they do not replace runtime approval."])

    async def stage_price_update(self, session, items, note=None):
        binding = self._scope(session)
        if len(items) != 1:
            raise ChangeNotApplicable("Protected execution currently supports one price target; use the runtime API for bulk evaluation")
        runtime = self._bound_runtime(session)
        current = await runtime.backend.get(items[0].listing_id)
        # Identical retries in the same wrapper/session reuse the exact canonical
        # request (including timestamp). New prices/state produce a new request.
        proposal = {"binding": binding, "id": items[0].listing_id, "before": str(current.price),
                    "after": str(items[0].new_price), "reason": note}
        key = hashlib.sha256(json.dumps(proposal, sort_keys=True).encode()).hexdigest()
        if key not in self._proposals:
            self._proposals[key] = CommerceAction(merchant_id=session.merchant_id, source="anthropic.merchant-agent",
                                                 principal=self._principal, agent=self._agent, delegation=self._delegation,
                                                 type="price.update", reason=note or "Merchant agent price proposal",
                                                 items=[{"resource_id": items[0].listing_id, "proposed_after": Decimal(str(items[0].new_price))}],
                                                 metadata={"protected_binding": binding})
        row = await runtime.evaluate(self._proposals[key])
        return self._change(row)

    async def apply_change(self, session, change_id):
        row = self._runtime.get_action(change_id)
        self._owned(session, row)
        try:
            row = await self._bound_runtime(session).execute(change_id, actor=self._principal.subject or "unknown")
        except DomainError as error:
            raise ChangeNotApplicable(str(error)) from None
        return self._change(row)

    async def get_pending_changes(self, session):
        self._scope(session)
        rows = self._runtime.list_actions(limit=500)
        changes = []
        for row in rows:
            try:
                self._owned(session, row)
            except PermissionError:
                continue
            if row["state"] in {"allowed", "waiting_for_approval", "approved"}:
                changes.append(self._change(row))
        return changes

    async def discard_change(self, session, change_id, actor_kind=None):
        raise ChangeNotApplicable("Reject through the separately authenticated approval API/UI")

    async def _read(self, name, session, *args, **kwargs):
        self._scope(session)
        logger.info(json.dumps({"event": "read.delegated", "action_id": str(uuid.uuid4()),
                                "trace_id": str(uuid.uuid4()), "method": name, "merchant_id": session.merchant_id}))
        return await getattr(self._backend, name)(session, *args, **kwargs)

    async def get_listing(self, session, listing_id):
        return await self._read("get_listing", session, listing_id)

    async def get_pricing_context(self, session, listing_id):
        return await self._read("get_pricing_context", session, listing_id)

    async def search_listings(self, session, query, filters=None, limit=8):
        return await self._read("search_listings", session, query, filters, limit)

    async def get_business_snapshot(self, session, period=None):
        return await self._read("get_business_snapshot", session, period)

    async def query_metrics(self, session, metric, period=None, granularity="day", segment=None):
        return await self._read("query_metrics", session, metric, period, granularity, segment)

    async def get_campaign_performance(self, session, campaign_id=None):
        return await self._read("get_campaign_performance", session, campaign_id)

    async def get_inventory_alerts(self, session):
        return await self._read("get_inventory_alerts", session)

    async def get_order_issues(self, session):
        return await self._read("get_order_issues", session)

    async def get_merchant_context(self, session):
        return await self._read("get_merchant_context", session)

    async def get_analysis_schema(self, session):
        return await self._read("get_analysis_schema", session)

    async def execute_analysis_query(self, session, sql):
        # The original backend must enforce its upstream read-only SQL contract.
        return await self._read("execute_analysis_query", session, sql)

    async def _unsupported(self, *args, **kwargs):
        raise ChangeNotApplicable("This write is not connected to Auteric policy enforcement; denied")

    stage_listing_update = _unsupported
    stage_inventory_action = _unsupported
    stage_promotion = _unsupported
    stage_campaign = _unsupported


def protect(existing_backend, *, principal=None, agent=None, runtime, delegation=None, backend_id=None):
    """Wrap an existing backend; no model changes, copied source, or merchant replacement."""
    return AutericProtectedBackend(existing_backend, runtime=runtime, principal=principal, agent=agent,
                                   delegation=delegation, backend_id=backend_id)
