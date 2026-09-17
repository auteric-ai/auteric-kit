"""Small deterministic mapping language. No eval, scripts, templates or LLM calls."""

import hashlib
import json
import re
from datetime import datetime
from decimal import Decimal
from typing import Any, Literal
from urllib.parse import quote, urlsplit

import httpx
from pydantic import BaseModel, ConfigDict, Field, model_validator

from .http import request_json
from .models import INPUTS, validate_input, validate_output


class Transform(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source: str | None = None
    kind: Literal["identity", "string", "number", "integer", "currency", "enum", "date", "concat", "array"] = "identity"
    constant: Any = None
    default: Any = None
    values: dict[str, Any] = Field(default_factory=dict)
    fields: dict[str, "Transform"] = Field(default_factory=dict)
    sources: list[str] = Field(default_factory=list)
    separator: str = ""


class Credential(BaseModel):
    model_config = ConfigDict(extra="forbid")
    env: str = Field(pattern=r"^[A-Z][A-Z0-9_]{0,100}$")
    prefix: str = ""


class Mapping(BaseModel):
    model_config = ConfigDict(extra="forbid")
    operation: str
    kind: Literal["rest", "sdk"] = "rest"
    version: int = Field(default=1, ge=1)
    method: Literal["GET", "POST", "PUT", "PATCH", "DELETE"] | None = None
    path: str | None = Field(default=None, min_length=1, max_length=1000)
    request: dict[str, dict[str, Transform]] = Field(default_factory=dict)
    response: dict[str, Transform] = Field(default_factory=dict)
    response_root: str | None = None
    credential_headers: dict[str, Credential] = Field(default_factory=dict)

    @model_validator(mode="after")
    def secure_shape(self):
        if self.operation not in INPUTS:
            raise ValueError("Unsupported operation")
        if self.kind == "sdk":
            if any((self.method, self.path, self.request, self.response, self.response_root, self.credential_headers)):
                raise ValueError("SDK mappings select only a canonical operation; code and credentials stay local")
            return self
        if not self.method or not self.path:
            raise ValueError("REST mappings require method and path")
        if (
            not self.path.startswith("/")
            or self.path.startswith("//")
            or any(c in self.path for c in ("?", "#", "\\", "%"))
        ):
            raise ValueError("Use a relative absolute-path template without query, fragments or encoding")
        if any(p in {".", ".."} for p in self.path.split("/")):
            raise ValueError("Path traversal is forbidden")
        if set(self.request) - {"path", "query", "body", "headers"}:
            raise ValueError("Unknown request mapping target")
        for name in {*self.request.get("headers", {}), *self.credential_headers}:
            if not re.fullmatch(r"[A-Za-z0-9-]+", name) or name.lower() in {
                "host",
                "cookie",
                "content-length",
                "transfer-encoding",
                "connection",
            }:
                raise ValueError("Unsafe header mapping")
        if any(n.lower() == "authorization" for n in self.request.get("headers", {})):
            raise ValueError("Authorization must use local credential_headers")
        sensitive = re.compile(r'(?:api[-_]?key|token|secret|password|authorization)', re.I)
        for location, fields in self.request.items():
            for name, transform_rule in fields.items():
                if sensitive.search(name) and (transform_rule.constant is not None or transform_rule.default is not None):
                    raise ValueError('Credential-like literals are forbidden; use local credential references')
        return self


def validate_mapping(mapping):
    return Mapping.model_validate(mapping).model_dump(mode="json", exclude_none=True)


def mapping_digest(mapping):
    return hashlib.sha256(
        json.dumps(validate_mapping(mapping), sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def lookup(data, path):
    if path in ("", "$", "response"):
        return data
    path = path.removeprefix("response.")
    value = data
    for part in path.split("."):
        value = value[int(part)] if isinstance(value, list) else value[part]
    return value


def transform(rule, data):
    rule = Transform.model_validate(rule)
    if "constant" in rule.model_fields_set:
        value = rule.constant
    elif rule.kind == "concat":
        return rule.separator.join(str(lookup(data, source)) for source in rule.sources)
    else:
        try:
            value = lookup(data, rule.source or "$")
        except (KeyError, IndexError, TypeError, ValueError):
            if "default" not in rule.model_fields_set:
                raise ValueError("Required mapped field is missing") from None
            value = rule.default
    if rule.kind == "identity":
        return value
    if rule.kind == "string":
        return str(value)
    if rule.kind == "number":
        number = Decimal(str(value))
        if not number.is_finite():
            raise ValueError("Non-finite mapping value")
        return str(number)
    if rule.kind == "integer":
        number = Decimal(str(value))
        if not number.is_finite() or number != number.to_integral_value():
            raise ValueError("Non-integer mapping value")
        return int(number)
    if rule.kind == "currency":
        return str(value).upper()
    if rule.kind == "enum":
        if str(value) not in rule.values:
            raise ValueError("Unmapped enum value")
        return rule.values[str(value)]
    if rule.kind == "date":
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.utcoffset() is None:
            raise ValueError("Date requires timezone")
        return parsed.isoformat()
    if rule.kind == "array":
        if not isinstance(value, list):
            raise ValueError("Expected array")
        return [map_fields(rule.fields, item) for item in value]
    raise ValueError("Unsupported transformation")


def map_fields(fields, data):
    result = {}
    for target, rule in fields.items():
        node = result
        parts = target.split(".")
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node[parts[-1]] = transform(rule, data)
    return result


def safe_base_url(url, *, allow_loopback=False):
    parsed = urlsplit(url)
    if parsed.username or parsed.password or parsed.query or parsed.fragment or not parsed.hostname:
        raise ValueError("Use a credential-free base URL")
    if parsed.scheme != "https" and not (
        allow_loopback and parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1", "::1"}
    ):
        raise ValueError("TLS is required; loopback HTTP needs explicit development mode")
    return url.rstrip("/")


class MappedConnector:
    """Base URL and secret allowlist are local configuration, never SaaS job input."""

    def __init__(
        self,
        base_url,
        *,
        credentials=None,
        allowed_paths=None,
        approved_mapping_digests=None,
        allow_loopback=False,
        transport=None,
    ):
        self.base_url = safe_base_url(base_url, allow_loopback=allow_loopback)
        self.credentials = credentials or {}
        self.allowed_paths = frozenset(allowed_paths or [])
        self.approved_mapping_digests = frozenset(approved_mapping_digests or [])
        if not self.allowed_paths:
            raise ValueError("Review and allowlist merchant path templates locally")
        self.client = httpx.AsyncClient(timeout=15, follow_redirects=False, trust_env=False, transport=transport)

    async def execute(self, operation, request, mapping=None):
        rule = Mapping.model_validate(mapping)
        if rule.kind != "rest":
            raise ValueError("Use a locally installed factory connector for SDK mappings")
        if operation != rule.operation or rule.path not in self.allowed_paths:
            raise ValueError("Operation or local path allowlist mismatch")
        if self.approved_mapping_digests and mapping_digest(mapping) not in self.approved_mapping_digests:
            raise ValueError("Mapping does not match a locally pinned approved digest")
        if operation in {"search_products", "get_product", "get_cart", "get_checkout"} and rule.method != "GET":
            raise ValueError("Read operations require GET in this REST adapter")
        data = validate_input(operation, request)
        path = rule.path
        for name, value in map_fields(rule.request.get("path", {}), data).items():
            value = str(value)
            if value in {".", ".."} or any(c in value for c in ("/", "\\", "%", "?", "#")):
                raise ValueError("Unsafe path parameter")
            path = path.replace("{" + name + "}", quote(value, safe=""))
        if "{" in path or "}" in path:
            raise ValueError("Unresolved path parameter")
        headers = map_fields(rule.request.get("headers", {}), data)
        for name, credential in rule.credential_headers.items():
            if credential.env not in self.credentials:
                raise ValueError("Required local credential reference is unavailable")
            headers[name] = credential.prefix + self.credentials[credential.env]
        if any("\r" in str(v) or "\n" in str(v) for v in headers.values()):
            raise ValueError("Unsafe header value")
        kwargs = {"params": map_fields(rule.request.get("query", {}), data), "headers": headers}
        if "body" in rule.request:
            kwargs["json"] = map_fields(rule.request["body"], data)
        raw = await request_json(self.client, rule.method, self.base_url + path, **kwargs)
        if rule.response_root:
            raw = lookup(raw, rule.response_root)
        if rule.response:
            output = (
                [map_fields(rule.response, item) for item in raw]
                if operation == "search_products"
                else map_fields(rule.response, raw)
            )
        else:
            output = raw
        return validate_output(operation, output)

    async def close(self):
        await self.client.aclose()


def suggest_mappings(document):
    """Heuristics only. Draft confidence is not validation or activation."""
    if not (str(document.get("openapi", "")).startswith("3.") or document.get("swagger") == "2.0"):
        raise ValueError("Expected OpenAPI 3.x or Swagger 2.0")
    suggestions = []
    aliases = {
        "searchproducts": "search_products",
        "listproducts": "search_products",
        "getproduct": "get_product",
        "createcart": "create_cart",
        "getcart": "get_cart",
        "addtocart": "add_to_cart",
        "addcartitem": "add_to_cart",
        "updatecartitem": "update_cart_item",
        "removefromcart": "remove_from_cart",
        "removecartitem": "remove_from_cart",
        "createcheckout": "create_checkout",
        "getcheckout": "get_checkout",
        "replacecartitems": "replace_cart_items",
        "cancelcart": "cancel_cart",
    }
    for path, entry in document.get("paths", {}).items():
        for method, endpoint in entry.items():
            if method.lower() not in {"get", "post", "put", "patch", "delete"}:
                continue
            operation = aliases.get(re.sub(r"[^a-z]", "", endpoint.get("operationId", "").lower()))
            if not operation:
                continue
            params = entry.get("parameters", []) + endpoint.get("parameters", [])
            request = {}
            for param in params:
                location, name = param.get("in"), param.get("name")
                if location not in {"path", "query"} or not name:
                    continue
                canonical = {
                    "productId": "product_id",
                    "cartId": "cart_id",
                    "checkoutId": "checkout_id",
                    "q": "query",
                }.get(name, name)
                request.setdefault(location, {})[name] = {"source": canonical}
            body = endpoint.get("requestBody", {}).get("content", {}).get("application/json", {}).get("schema", {})
            if body.get("properties"):
                request["body"] = {
                    name: {"source": {"skuCode": "product_id", "qty": "quantity", "cartId": "cart_id"}.get(name, name)}
                    for name in body["properties"]
                }
            mapping = validate_mapping(
                {"operation": operation, "method": method.upper(), "path": path, "request": request}
            )
            suggestions.append(
                {
                    "operation": operation,
                    "mapping": mapping,
                    "confidence": 0.65,
                    "rationale": "operationId matched; response fields, references, authentication "
                    "and merchant semantics require developer review and contract tests.",
                }
            )
    return suggestions
