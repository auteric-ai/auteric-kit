"""Loopback-only synthetic storefront wired through the real Auteric gateway.

The lab is explicit test infrastructure. Browser code never receives gateway
credentials and cannot call the merchant connector directly.
"""

from __future__ import annotations

import asyncio
import contextlib
import re
import secrets
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field

from auteric_edge.worker import EdgeWorker

from .app import create_app
from .storage import digest

ASSETS = Path(__file__).with_name("local_lab_assets")
SDK_DIST = Path(__file__).resolve().parents[2] / "packages" / "lab-sdk" / "dist"
OPERATIONS = frozenset(
    {
        "search_products",
        "get_product",
        "create_cart",
        "get_cart",
        "add_to_cart",
        "update_cart_item",
        "remove_from_cart",
        "create_checkout",
        "get_checkout",
        "update_checkout",
        "complete_checkout",
        "cancel_checkout",
        "get_order",
        "apply_discount_code",
        "remove_discount_code",
        "get_shipping_options",
        "set_shipping_address",
        "select_shipping_option",
    }
)
READS = frozenset({"search_products", "get_product", "get_cart", "get_checkout", "get_order", "get_shipping_options"})


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ExecuteRequest(StrictModel):
    operation: str = Field(min_length=1, max_length=80)
    input: dict[str, Any] = Field(default_factory=dict)
    request_id: str = Field(min_length=8, max_length=200, pattern=r"^[A-Za-z0-9_.:-]+$")


class ApprovalRequest(StrictModel):
    request_id: str = Field(min_length=8, max_length=200, pattern=r"^[A-Za-z0-9_.:-]+$")
    approval_id: str = Field(min_length=16, max_length=200)


class PriceChange(StrictModel):
    price: Literal["100.00", "120.00", "200.00"]


@dataclass
class BrowserSession:
    gateway_session: str
    csrf: str
    expires: float
    pending: dict[str, dict[str, Any]] = field(default_factory=dict)
    requests: dict[str, dict[str, Any]] = field(default_factory=dict)


@dataclass
class LabRuntime:
    gateway: FastAPI
    admin: httpx.AsyncClient
    worker: EdgeWorker
    worker_task: asyncio.Task
    worker_stop: asyncio.Event
    merchant: Any
    store_id: str
    agent_token: str
    sessions: dict[str, BrowserSession] = field(default_factory=dict)


def _gateway_reason(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return "Gateway refused the request without a structured reason"
    if isinstance(body, dict):
        detail = body.get("detail")
        if isinstance(detail, str):
            return detail
        decision = body.get("decision")
        if isinstance(decision, dict) and isinstance(decision.get("reasons"), list):
            return " ".join(str(value) for value in decision["reasons"])
    return "Gateway refused the request"


async def _post_action(runtime: LabRuntime, session: BrowserSession, body: ExecuteRequest) -> dict[str, Any]:
    session.requests[body.request_id] = {"operation": body.operation, "input": body.input, "created": time.time()}
    if len(session.requests) > 200:
        session.requests.pop(next(iter(session.requests)))
    headers = {
        "authorization": f"Bearer {runtime.agent_token}",
        "x-auteric-session": session.gateway_session,
        "idempotency-key": body.request_id,
    }
    try:
        response = await runtime.admin.post(
            f"/api/commerce/stores/{runtime.store_id}/actions/{body.operation}",
            headers=headers,
            json=body.input,
        )
    except (httpx.HTTPError, asyncio.TimeoutError):
        return {
            "status": "unknown",
            "request_id": body.request_id,
            "reason": "Gateway transport ended without a confirmed outcome; do not retry a write automatically",
        }
    payload = response.json() if response.headers.get("content-type", "").startswith("application/json") else {}
    if response.status_code == 200 and payload.get("state") == "executed":
        return {
            "status": "executed",
            "request_id": body.request_id,
            "result": payload.get("result"),
            **({"replayed": True} if payload.get("replayed") is True else {}),
        }
    if response.status_code == 202 and payload.get("state") == "waiting_for_approval":
        record = {
            "request_id": body.request_id,
            "operation": body.operation,
            "input": body.input,
            "approval_id": payload.get("action_id"),
            "digest": payload.get("digest"),
        }
        session.pending[body.request_id] = record
        reasons = payload.get("decision", {}).get("reasons", [])
        return {
            "status": "approval_required",
            "request_id": body.request_id,
            "approval_id": record["approval_id"],
            "reason": " ".join(str(value) for value in reasons) or "Exact operator approval is required",
        }
    if response.status_code >= 500:
        return {
            "status": "unknown",
            "request_id": body.request_id,
            "reason": "Gateway did not confirm execution; inspect local traffic before retrying",
        }
    code = {
        401: "agent_authentication",
        403: "policy_or_ownership",
        409: "conflict_or_reconciliation",
        422: "invalid_request",
        429: "rate_limit",
    }.get(response.status_code, "gateway_refused")
    return {
        "status": "blocked",
        "request_id": body.request_id,
        "reason": _gateway_reason(response),
        "code": code,
    }


async def _activate_mapping(runtime: LabRuntime, operation: str, sample: dict[str, Any]) -> None:
    root = f"/api/commerce/stores/{runtime.store_id}"
    draft = await runtime.admin.post(
        root + "/mappings", json={"operation": operation, "mapping": {"kind": "sdk"}}
    )
    draft.raise_for_status()
    version = draft.json()["id"]
    evidence = await runtime.admin.post(
        root + f"/mappings/{version}/test", json={"mode": "sandbox", "input": sample}
    )
    evidence.raise_for_status()
    if evidence.json().get("status") != "pass":
        raise RuntimeError(f"Synthetic mapping contract failed for {operation}: {evidence.text}")
    activation = await runtime.admin.post(root + f"/mappings/{version}/activate")
    activation.raise_for_status()


async def _bootstrap(database: str) -> LabRuntime:
    from .lab_merchant import LabMerchant

    gateway = create_app(database, public_url="http://127.0.0.1", development=True, job_timeout=3)
    transport = httpx.ASGITransport(app=gateway)
    admin = httpx.AsyncClient(
        transport=transport,
        base_url="http://127.0.0.1",
        headers={"x-auteric-console": "1"},
        timeout=8,
        follow_redirects=False,
    )
    created = await admin.post(
        "/api/commerce/auth/register",
        json={
            "email": f"lab-{secrets.token_hex(8)}@local.invalid",
            "password": secrets.token_urlsafe(32),
            "organization": "Auteric Synthetic Lab",
        },
    )
    created.raise_for_status()
    store_response = await admin.post(
        "/api/commerce/stores",
        json={"name": "Synthetic Store", "domain": "store.local.invalid", "environment": "sandbox"},
    )
    store_response.raise_for_status()
    store_id = store_response.json()["id"]
    root = f"/api/commerce/stores/{store_id}"
    connector_token_response = await admin.post(root + "/connector-token")
    connector_token_response.raise_for_status()
    connector_token = connector_token_response.json()["token"]
    merchant = LabMerchant()
    worker = EdgeWorker(
        merchant,
        api_url="http://127.0.0.1",
        store_id=store_id,
        token=connector_token,
        database=database + ".edge",
        environment="sandbox",
        allow_loopback=True,
        transport=httpx.ASGITransport(app=gateway),
    )
    stop = asyncio.Event()

    async def work() -> None:
        while not stop.is_set():
            delay = 0.5
            try:
                await worker.tick()
            except httpx.HTTPStatusError as exc:
                # The control plane allows 180 polls/minute. A one-second 429
                # backoff prevents an idle worker from sustaining a rejection loop.
                if exc.response.status_code == 429:
                    delay = 1.0
            except (httpx.HTTPError, ValueError):
                pass
            try:
                await asyncio.wait_for(stop.wait(), delay)
            except TimeoutError:
                pass

    worker_task = asyncio.create_task(work(), name="auteric-local-lab-edge")
    runtime = LabRuntime(gateway, admin, worker, worker_task, stop, merchant, store_id, "")
    try:
        # Synthetic contract fixtures are merchant-local only. All mapping states
        # still pass through draft -> test -> activate public control APIs.
        fixture_cart = await merchant.execute(
            "create_cart", {"currency": "USD", "items": [{"product_id": "demo-shoes", "quantity": 1}]}
        )
        add_cart = await merchant.execute("create_cart", {"currency": "USD", "items": []})
        update_cart = await merchant.execute(
            "create_cart", {"currency": "USD", "items": [{"product_id": "demo-shoes", "quantity": 1}]}
        )
        remove_cart = await merchant.execute(
            "create_cart", {"currency": "USD", "items": [{"product_id": "demo-shoes", "quantity": 1}]}
        )
        checkout_cart = await merchant.execute(
            "create_cart", {"currency": "USD", "items": [{"product_id": "demo-shoes", "quantity": 1}]}
        )
        fixture_checkout = await merchant.execute("create_checkout", {"cart_id": checkout_cart["id"]})
        update_cart = await merchant.execute(
            "create_cart", {"currency": "USD", "items": [{"product_id": "demo-shoes", "quantity": 1}]}
        )
        update_checkout = await merchant.execute("create_checkout", {"cart_id": update_cart["id"]})
        cancel_cart_fixture = await merchant.execute(
            "create_cart", {"currency": "USD", "items": [{"product_id": "demo-shoes", "quantity": 1}]}
        )
        cancel_checkout = await merchant.execute("create_checkout", {"cart_id": cancel_cart_fixture["id"]})
        complete_cart = await merchant.execute(
            "create_cart", {"currency": "USD", "items": [{"product_id": "demo-shoes", "quantity": 1}]}
        )
        complete_checkout = await merchant.execute("create_checkout", {"cart_id": complete_cart["id"]})
        await merchant.execute("update_checkout", {
            "checkout_id": complete_checkout["id"], "items": [{"product_id": "demo-shoes", "quantity": 1}],
            "buyer": {}, "context": {}, "fulfillment": {},
        })
        completed = await merchant.execute("complete_checkout", {
            "checkout_id": complete_checkout["id"], "payment": {"handler_id": "sandbox"},
        })
        fixture_order_id = completed["metadata"]["order_id"]
        completion_sample_cart = await merchant.execute(
            "create_cart", {"currency": "USD", "items": [{"product_id": "demo-shoes", "quantity": 1}]}
        )
        completion_sample = await merchant.execute("create_checkout", {"cart_id": completion_sample_cart["id"]})
        await merchant.execute("update_checkout", {
            "checkout_id": completion_sample["id"], "items": [{"product_id": "demo-shoes", "quantity": 1}],
            "buyer": {}, "context": {}, "fulfillment": {},
        })
        discount_cart = await merchant.execute(
            "create_cart", {"currency": "USD", "items": [{"product_id": "demo-shoes", "quantity": 1}]}
        )
        remove_discount_cart = await merchant.execute(
            "create_cart", {"currency": "USD", "items": [{"product_id": "demo-shoes", "quantity": 1}]}
        )
        await merchant.execute("apply_discount_code", {"cart_id": remove_discount_cart["id"], "code": "SAVE10"})
        shipping_cart = await merchant.execute(
            "create_cart", {"currency": "USD", "items": [{"product_id": "demo-shoes", "quantity": 1}]}
        )
        address_checkout = await merchant.execute("create_checkout", {"cart_id": shipping_cart["id"]})
        option_cart = await merchant.execute(
            "create_cart", {"currency": "USD", "items": [{"product_id": "demo-shoes", "quantity": 1}]}
        )
        option_checkout = await merchant.execute("create_checkout", {"cart_id": option_cart["id"]})
        samples = {
            "search_products": {"query": "", "limit": 10},
            "get_product": {"product_id": "demo-shoes"},
            "create_cart": {"currency": "USD", "items": []},
            "get_cart": {"cart_id": fixture_cart["id"]},
            "add_to_cart": {"cart_id": add_cart["id"], "product_id": "demo-shoes", "quantity": 1},
            "update_cart_item": {"cart_id": update_cart["id"], "product_id": "demo-shoes", "quantity": 1},
            "remove_from_cart": {"cart_id": remove_cart["id"], "product_id": "demo-shoes"},
            "create_checkout": {"cart_id": checkout_cart["id"]},
            "get_checkout": {"checkout_id": fixture_checkout["id"]},
            "update_checkout": {"checkout_id": update_checkout["id"], "items": [{"product_id": "demo-shoes", "quantity": 1}], "buyer": {}, "context": {}, "fulfillment": {}},
            "complete_checkout": {"checkout_id": completion_sample["id"], "payment": {"handler_id": "sandbox"}},
            "cancel_checkout": {"checkout_id": cancel_checkout["id"]},
            "get_order": {"order_id": fixture_order_id},
            "apply_discount_code": {"cart_id": discount_cart["id"], "code": "SAVE10"},
            "remove_discount_code": {"cart_id": remove_discount_cart["id"], "code": "SAVE10"},
            "get_shipping_options": {"cart_id": shipping_cart["id"], "address": {}},
            "set_shipping_address": {"checkout_id": address_checkout["id"], "address": {"country": "US"}},
            "select_shipping_option": {"checkout_id": option_checkout["id"], "option_id": "standard"},
        }
        for operation in (
            "search_products",
            "get_product",
            "create_cart",
            "get_cart",
            "add_to_cart",
            "update_cart_item",
            "remove_from_cart",
            "create_checkout",
            "get_checkout",
            "update_checkout",
            "complete_checkout",
            "cancel_checkout",
            "get_order",
            "apply_discount_code",
            "remove_discount_code",
            "get_shipping_options",
            "set_shipping_address",
            "select_shipping_option",
        ):
            await _activate_mapping(runtime, operation, samples[operation])
        policy = await admin.put(root + "/policy", json={"approval_cart_value": "150"})
        policy.raise_for_status()
        agent = await admin.post(root + "/agent-token")
        agent.raise_for_status()
        runtime.agent_token = agent.json()["token"]
        # Explicit synthetic fixture only. This lab cannot verify a public domain;
        # the production control plane has no equivalent bypass endpoint.
        with gateway.state.store.db() as db:
            domain = db.execute("SELECT domain FROM stores WHERE id=?", (store_id,)).fetchone()[0]
            db.execute("UPDATE stores SET agent_access_enabled=1,verified=? WHERE id=?", (time.time(), store_id))
            db.execute("INSERT OR REPLACE INTO routing_bindings VALUES(?,?,?,?,?)", (domain, store_id, "synthetic_lab", time.time(), time.time()))
        return runtime
    except BaseException:
        stop.set()
        worker_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await worker_task
        await worker.close()
        await admin.aclose()
        raise


def create_local_lab(database: str = ".runtime/commerce/local-lab.db") -> FastAPI:
    """Create an isolated synthetic lab. Serve only on a loopback bind."""

    database = str(Path(database))

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        runtime = await _bootstrap(database)
        app.state.runtime = runtime
        try:
            yield
        finally:
            runtime.worker_stop.set()
            runtime.worker_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await runtime.worker_task
            await runtime.worker.close()
            await runtime.admin.aclose()

    app = FastAPI(title="Auteric Synthetic Store Lab", version="0.1.0", lifespan=lifespan)

    @app.middleware("http")
    async def local_security(request: Request, call_next):
        host = request.url.hostname
        if host not in {"localhost", "127.0.0.1", "::1", "testserver"}:
            return JSONResponse({"detail": "The synthetic lab is loopback-only"}, status_code=400)
        response = await call_next(request)
        response.headers.update(
            {
                "cache-control": "no-store",
                "content-security-policy": "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'",
                "referrer-policy": "no-referrer",
                "x-content-type-options": "nosniff",
                "x-frame-options": "DENY",
            }
        )
        return response

    def browser_session(request: Request, *, csrf: bool = False) -> tuple[str, BrowserSession]:
        token = request.cookies.get("auteric_lab_session", "")
        session = app.state.runtime.sessions.get(token)
        if not session or session.expires <= time.time():
            if token:
                app.state.runtime.sessions.pop(token, None)
            raise HTTPException(401, "Initialize a fresh local agent session")
        if csrf and not secrets.compare_digest(request.headers.get("x-auteric-csrf", ""), session.csrf):
            raise HTTPException(403, "Valid local CSRF token required")
        return token, session

    @app.get("/api/auteric/bootstrap")
    async def bootstrap(request: Request):
        runtime: LabRuntime = app.state.runtime
        token = request.cookies.get("auteric_lab_session", "")
        session = runtime.sessions.get(token)
        if not session or session.expires <= time.time():
            token = secrets.token_urlsafe(32)
            response = await runtime.admin.post(
                f"/ucp/{runtime.store_id}/sessions", headers={"authorization": f"Bearer {runtime.agent_token}"}
            )
            response.raise_for_status()
            session = BrowserSession(response.json()["session_id"], secrets.token_urlsafe(32), time.time() + 3600)
            runtime.sessions[token] = session
        result = JSONResponse(
            {
                "mode": "synthetic-local-lab",
                "store": "Auteric Synthetic Store",
                "csrf": session.csrf,
                "operations": sorted(OPERATIONS),
                "production_ready": False,
            }
        )
        result.set_cookie(
            "auteric_lab_session", token, httponly=True, secure=False, samesite="strict", max_age=3600, path="/"
        )
        return result

    @app.post("/api/auteric/execute")
    async def execute(body: ExecuteRequest, request: Request):
        _, session = browser_session(request)
        if (
            request.headers.get("x-auteric-request") != "gateway-v1"
            or request.headers.get("idempotency-key") != body.request_id
        ):
            return {
                "status": "blocked",
                "request_id": body.request_id,
                "reason": "Browser request headers are not bound to the exact request_id",
                "code": "request_binding",
            }
        if body.operation not in OPERATIONS:
            return {
                "status": "blocked",
                "request_id": body.request_id,
                "reason": "Operation is not part of the nine-operation local contract",
                "code": "unsupported_operation",
            }
        return await _post_action(app.state.runtime, session, body)

    @app.post("/api/auteric/approve")
    async def approve(body: ApprovalRequest, request: Request):
        _, session = browser_session(request, csrf=True)
        pending = session.pending.get(body.request_id)
        if (
            not pending
            or pending["approval_id"] != body.approval_id
        ):
            return {
                "status": "blocked",
                "request_id": body.request_id,
                "reason": "Approval is not bound to this browser session and exact pending request",
                "code": "approval_binding",
            }
        runtime: LabRuntime = app.state.runtime
        response = await runtime.admin.post(
            f"/api/commerce/stores/{runtime.store_id}/approvals/{body.approval_id}/approve",
            json={"digest": pending["digest"]},
        )
        if response.status_code != 200:
            return {
                "status": "blocked",
                "request_id": body.request_id,
                "reason": _gateway_reason(response),
                "code": "approval_refused",
            }
        resumed = await _post_action(
            runtime,
            session,
            ExecuteRequest(operation=pending["operation"], input=pending["input"], request_id=body.request_id),
        )
        if resumed["status"] == "executed":
            session.pending.pop(body.request_id, None)
        return resumed

    @app.get("/api/auteric/traffic")
    async def traffic(request: Request):
        _, session = browser_session(request)
        runtime: LabRuntime = app.state.runtime
        response = await runtime.admin.get(f"/api/commerce/stores/{runtime.store_id}/traffic")
        response.raise_for_status()
        session_hash = digest(session.gateway_session)
        rows = [row for row in response.json() if row.get("session") == session_hash]
        return {"mode": "synthetic-local-lab", "traffic": rows}

    @app.post("/api/auteric/lab/faults/lost-write")
    async def arm_lost_write(request: Request):
        browser_session(request, csrf=True)
        app.state.runtime.merchant.fail_next_write = True
        return {
            "mode": "synthetic-local-lab",
            "armed": "next-write-response-loss",
            "warning": "The next synthetic merchant write will commit, then lose its response",
        }

    @app.put("/api/auteric/lab/products/demo-shoes/price")
    async def change_price(body: PriceChange, request: Request):
        browser_session(request, csrf=True)
        product = app.state.runtime.merchant.products["demo-shoes"]
        previous = product["price"]
        product["price"] = body.price
        return {
            "mode": "synthetic-local-lab",
            "product_id": "demo-shoes",
            "previous": previous,
            "price": body.price,
        }

    @app.post("/api/auteric/lab/test-transaction")
    async def test_product_transaction(request: Request):
        browser_session(request, csrf=True)
        runtime: LabRuntime = app.state.runtime
        response = await runtime.admin.post(
            f"/api/commerce/stores/{runtime.store_id}/test-transaction",
            headers={"x-auteric-console": "1"},
            json={"product_id": "demo-shoes"},
        )
        payload = response.json()
        return JSONResponse(payload, status_code=response.status_code)

    @app.get("/api/auteric/reconcile/{request_id}")
    async def reconcile(request_id: str, request: Request):
        if not re.fullmatch(r"[A-Za-z0-9_.:-]{8,200}", request_id):
            raise HTTPException(422, "Invalid request_id")
        _, session = browser_session(request)
        if request_id not in session.requests:
            raise HTTPException(404, "Request is not owned by this browser session")
        runtime: LabRuntime = app.state.runtime
        response = await runtime.admin.get(f"/api/commerce/stores/{runtime.store_id}/traffic")
        response.raise_for_status()
        action_id = digest(runtime.store_id + digest(runtime.agent_token) + digest(session.gateway_session) + request_id)
        row = next(
            (item for item in response.json() if item.get("id") == action_id and item.get("session") == digest(session.gateway_session)),
            None,
        )
        if not row:
            operation = session.requests[request_id]["operation"]
            return {
                "mode": "read-only-reconciliation",
                "request_id": request_id,
                "status": "not_observed",
                "new_request_safe": operation in READS,
                "replay_same_request_only": operation not in READS,
            }
        confirmed = row.get("state") == "executed"
        operation = session.requests[request_id]["operation"]
        result = {
            "mode": "read-only-reconciliation",
            "request_id": request_id,
            "status": "confirmed" if confirmed else "unresolved",
            "gateway_state": row.get("state"),
            "new_request_safe": operation in READS,
            "replay_same_request_only": operation not in READS,
            "guidance": "Do not retry an unresolved write; inspect merchant state and gateway evidence",
        }
        cart_id = session.requests[request_id]["input"].get("cart_id")
        if cart_id and operation not in READS:
            snapshot_id = "reconcile:" + secrets.token_hex(12)
            snapshot = await _post_action(
                runtime,
                session,
                ExecuteRequest(operation="get_cart", input={"cart_id": cart_id}, request_id=snapshot_id),
            )
            if snapshot["status"] == "executed":
                result["observed_cart"] = snapshot["result"]
                result["evidence_limit"] = (
                    "Fresh merchant cart snapshot only; it does not prove which exact write produced this state"
                )
        return result

    app.mount("/lab-assets", StaticFiles(directory=ASSETS), name="local-lab-assets")
    if SDK_DIST.is_dir():
        app.mount("/lab-sdk", StaticFiles(directory=SDK_DIST), name="local-lab-webmcp-sdk")

    @app.get("/")
    @app.get("/console")
    def storefront():
        return FileResponse(ASSETS / "index.html")

    return app


def main() -> None:
    import argparse

    import uvicorn

    parser = argparse.ArgumentParser(description="Run the synthetic Auteric storefront lab on loopback")
    parser.add_argument("--host", default="127.0.0.1", choices=["127.0.0.1", "localhost", "::1"])
    parser.add_argument("--port", type=int, default=8091)
    parser.add_argument("--database", default=".runtime/commerce/local-lab.db")
    args = parser.parse_args()
    uvicorn.run(create_local_lab(args.database), host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
