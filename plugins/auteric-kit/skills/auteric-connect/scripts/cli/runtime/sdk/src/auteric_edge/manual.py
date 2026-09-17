"""Trusted local SDK loading. Nothing in a SaaS job selects/imports Python code."""

import importlib
import inspect
import re

from .connector import CommerceConnector
from .mapping import Mapping
from .models import INPUTS

REQUIRED_OPERATIONS = (
    "search_products",
    "get_product",
    "create_cart",
    "get_cart",
    "add_to_cart",
    "update_cart_item",
    "remove_from_cart",
    "create_checkout",
    "get_checkout",
)


class ManualConnector:
    """Restrict remote jobs to reviewed canonical names and local fixed handlers."""

    def __init__(self, implementation):
        if not isinstance(implementation, CommerceConnector):
            raise TypeError("Factory must return a CommerceConnector instance")
        for operation in REQUIRED_OPERATIONS:
            handler = getattr(implementation, operation)
            if not inspect.iscoroutinefunction(handler):
                raise TypeError(f"{operation} must be async def {operation}(self, request)")
            inspect.signature(handler).bind({})
        self.implementation = implementation
        self.operations = list(REQUIRED_OPERATIONS)
        declared = getattr(implementation, "supported_operations", None)
        if declared is not None:
            if not set(declared) <= set(INPUTS):
                raise ValueError("Connector declares unknown operations")
            self.operations = [op for op in self.operations if op in declared]
        for operation in set(INPUTS) - set(REQUIRED_OPERATIONS):
            handler = getattr(type(implementation), operation, None)
            if (
                (declared is None or operation in declared)
                and handler is not getattr(CommerceConnector, operation, None)
                and inspect.iscoroutinefunction(handler)
            ):
                inspect.signature(getattr(implementation, operation)).bind({})
                self.operations.append(operation)

    async def execute(self, operation, request, mapping=None):
        rule = Mapping.model_validate(mapping)
        if rule.kind != "sdk" or rule.operation != operation or operation not in self.operations:
            raise ValueError("Job must select an implemented, reviewed SDK operation")
        # Call the SDK validator, not a customer override of the dispatch method.
        return await CommerceConnector.execute(self.implementation, operation, request)

    async def close(self):
        close = getattr(self.implementation, "close", None)
        if close is not None:
            result = close()
            if inspect.isawaitable(result):
                await result


def load_factory(reference):
    """Explicit local CLI choice, equivalent to running a trusted installed program.

    Factories take no arguments and get merchant secrets from local configuration.
    This function must never be exposed to the control-plane request payload.
    """
    if not re.fullmatch(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*:[A-Za-z_]\w*", reference):
        raise ValueError("Use an installed local module:factory name, not a URL, path or expression")
    module, name = reference.split(":")
    factory = getattr(importlib.import_module(module), name)
    if not callable(factory) or inspect.iscoroutinefunction(factory):
        raise TypeError("Factory must be a synchronous zero-argument function returning CommerceConnector")
    inspect.signature(factory).bind()
    return ManualConnector(factory())
