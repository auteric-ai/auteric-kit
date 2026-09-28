"""Review artifacts, not an execution engine or an authorization authority."""

import hashlib
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import PurePosixPath
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

Operation = Literal[
    "search_products",
    "get_product",
    "create_cart",
    "get_cart",
    "add_to_cart",
    "update_cart_item",
    "remove_from_cart",
    "replace_cart_items",
    "cancel_cart",
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
]
OPERATIONS = frozenset(Operation.__args__)
Text = Annotated[str, Field(min_length=1, max_length=2000)]
Digest = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]


class Artifact(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, revalidate_instances="always")

    @model_validator(mode="after")
    def reject_obvious_secrets(self):
        serialized = json.dumps(self.model_dump(mode="json"))
        if re.search(
            r"-----BEGIN .*PRIVATE KEY|Bearer\s+[A-Za-z0-9._-]{8,}|"
            r"(?:sk_live_|sk_test_|ghp_|github_pat_|AKIA)[A-Za-z0-9_]{12,}|"
            r"(?:password|secret|access_token|api_key)\s*[=:]\s*[^\s]{4,}",
            serialized,
            re.IGNORECASE,
        ):
            raise ValueError("Do not include credentials in review artifacts; use environment variable names")
        return self


def relative_path(value: str) -> str:
    path = PurePosixPath(value)
    if (
        not value
        or value != path.as_posix()
        or path.is_absolute()
        or any(part in {"..", "."} for part in value.split("/"))
        or "\\" in value
        or ":" in value
        or any(ord(c) < 32 for c in value)
        or any(part == ".git" or part == ".env" or part.startswith(".env.") for part in path.parts)
    ):
        raise ValueError("Use a normalized repository-relative non-secret path")
    return value


class Evidence(Artifact):
    path: str = Field(max_length=500)
    line: int = Field(ge=1, strict=True)
    explanation: Text

    _path = field_validator("path")(relative_path)


class OperationPlan(Artifact):
    operation: Operation
    status: Literal["mapped", "unsupported", "needs_review"]
    confidence: Literal["high", "medium", "low"]
    evidence: tuple[Evidence, ...] = Field(default=(), max_length=100)
    reason: Text
    interface: Text | None = None
    auth_session: Text | None = None
    side_effects: Text | None = None
    tests: tuple[Evidence, ...] = Field(default=(), max_length=100)

    @model_validator(mode="after")
    def check_mapping(self):
        if self.status == "mapped" and not (
            self.evidence and self.interface and self.auth_session and self.side_effects
        ):
            raise ValueError("Mapped operations require evidence, interface, auth/session and side effects")
        if self.status == "unsupported" and self.interface is not None:
            raise ValueError("Unsupported operations must not advertise an executable interface")
        return self


class IntegrationPlan(Artifact):
    schema_version: Literal[1] = 1
    plan_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,100}$")
    version: int = Field(ge=1, strict=True)
    repository_revision: Text
    operations: tuple[OperationPlan, ...] = Field(min_length=20, max_length=20)
    proposed_files: tuple[str, ...] = Field(default=(), max_length=200)
    data_egress: tuple[Text, ...] = Field(default=(), max_length=100)

    @field_validator("proposed_files")
    @classmethod
    def check_files(cls, values):
        for value in values:
            relative_path(value)
        if len(values) != len(set(values)):
            raise ValueError("Duplicate proposed files")
        return values

    @model_validator(mode="after")
    def complete_operations(self):
        if {item.operation for item in self.operations} != OPERATIONS:
            raise ValueError("Include each canonical operation exactly once")
        return self


def plan_digest(plan: IntegrationPlan) -> str:
    validated = IntegrationPlan.model_validate(plan)
    data = json.dumps(validated.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(data.encode()).hexdigest()


class PlanBinding(Artifact):
    plan_id: str
    plan_version: int = Field(ge=1, strict=True)
    plan_digest: Digest


class Approval(PlanBinding):
    approved: Literal[True]
    reviewer: Text
    approved_at: datetime

    @field_validator("approved", mode="before")
    @classmethod
    def explicit_boolean_approval(cls, value):
        if type(value) is not bool or value is not True:
            raise ValueError("Approval must be the explicit boolean true")
        return value

    @field_validator("reviewer")
    @classmethod
    def meaningful_reviewer(cls, value):
        value = value.strip()
        if not value:
            raise ValueError("Approval requires an authenticated reviewer identity")
        return value

    @field_validator("approved_at")
    @classmethod
    def timezone_required(cls, value):
        if value.tzinfo is None:
            raise ValueError("Approval timestamp requires timezone")
        if value > datetime.now(timezone.utc) + timedelta(minutes=5):
            raise ValueError("Approval timestamp cannot be in the future")
        return value


class OperationResult(Artifact):
    operation: Operation
    outcome: Literal["passed", "failed", "not_run", "unsupported"]
    evidence: tuple[Evidence, ...] = Field(default=(), max_length=100)
    details: Text

    @model_validator(mode="after")
    def passed_evidence(self):
        if self.outcome in {"passed", "failed"} and not self.evidence:
            raise ValueError("Executed results require test/report evidence")
        return self


class ValidationReport(PlanBinding):
    environment: Literal["mock", "local", "staging", "production"]
    results: tuple[OperationResult, ...] = Field(min_length=20, max_length=20)
    production_ready: Literal[False] = False

    @model_validator(mode="after")
    def complete_results(self):
        if {item.operation for item in self.results} != OPERATIONS:
            raise ValueError("Report every canonical operation exactly once")
        return self


def verify_review(
    plan: IntegrationPlan,
    approval: Approval,
    report: ValidationReport | None = None,
) -> dict:
    """Verify binding/consistency only; caller must authenticate reviewer and test provenance."""
    plan = IntegrationPlan.model_validate(plan)
    approval = Approval.model_validate(approval)
    if report is not None:
        report = ValidationReport.model_validate(report)
    expected = (plan.plan_id, plan.version, plan_digest(plan))
    for artifact in (approval, report):
        if artifact is not None and (artifact.plan_id, artifact.plan_version, artifact.plan_digest) != expected:
            raise ValueError("Stale or tampered review artifact: exact plan binding required")
    if report is not None:
        mappings = {item.operation: item.status for item in plan.operations}
        for result in report.results:
            if mappings[result.operation] == "unsupported" and result.outcome != "unsupported":
                raise ValueError("Unsupported operation cannot be reported as executed")
            if result.outcome == "passed" and mappings[result.operation] != "mapped":
                raise ValueError("Unreviewed operation cannot pass validation")
    return {
        "plan_digest": expected[2],
        "approval_matches": True,
        "validation_environment": report.environment if report else None,
        "validated_operations": [r.operation for r in report.results if r.outcome == "passed"] if report else [],
        "production_ready": False,
    }
