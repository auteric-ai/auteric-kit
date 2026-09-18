"""Deterministic, approval-gated Build Deep workflow artifacts.

This package deliberately does not call a model, run generated code, or apply files.
It records reviewable state and exact authorizations for an external, authenticated
executor to consume.
"""

from .store import (
    ApplyApproval,
    GenerationApproval,
    GenerationManifest,
    GenerationOutput,
    Stage,
    VerificationObservation,
    VerificationReport,
    Wiring,
    WorkflowState,
    WorkflowStore,
    generation_digest,
)
from .explorer_bridge import (
    BindingMap,
    ExplorerState,
    FirstPartyBinding,
    bridge_explorer_review,
    generate_bound_candidate,
    load_approved_explorer_state,
    load_binding_map,
    validate_bindings,
)

__all__ = [
    "ApplyApproval",
    "GenerationApproval",
    "GenerationManifest",
    "GenerationOutput",
    "Stage",
    "VerificationObservation",
    "VerificationReport",
    "Wiring",
    "WorkflowState",
    "WorkflowStore",
    "generation_digest",
    "BindingMap",
    "ExplorerState",
    "FirstPartyBinding",
    "bridge_explorer_review",
    "generate_bound_candidate",
    "load_approved_explorer_state",
    "load_binding_map",
    "validate_bindings",
]
