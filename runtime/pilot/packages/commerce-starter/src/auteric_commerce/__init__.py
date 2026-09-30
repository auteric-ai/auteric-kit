"""Provider-independent commerce security runtime; Anthropic is an optional adapter."""

from .actions import CommerceAction
from .policy import PolicyConfig
from .runtime import Runtime


def protect(backend, *, principal, agent, runtime, **kwargs):
    """Wrap an existing MerchantBackend without importing Anthropic into core."""
    from .protected_backend import protect as wrap
    return wrap(backend, principal=principal, agent=agent, runtime=runtime, **kwargs)


__all__ = ["CommerceAction", "PolicyConfig", "Runtime", "protect"]
