"""Filesystem state machine for an inspect/review/generate/verify workflow.

The event log is the authoritative state. Generated output is represented only by
paths and content digests; this module never creates or executes generated code.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

Text = Annotated[str, Field(min_length=1, max_length=2000)]
Digest = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
WorkflowId = Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,99}$")]

_SECRET_PATTERN = re.compile(
    r"-----BEGIN .*PRIVATE KEY|Bearer\s+[A-Za-z0-9._-]{8,}|"
    r"(?:sk_live_|sk_test_|ghp_|github_pat_|AKIA)[A-Za-z0-9_]{12,}|"
    r"(?:password|secret|access_token|api_key)\s*[=:]\s*[^\s]{4,}",
    re.IGNORECASE,
)


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def _reject_secrets(value: object) -> None:
    if _SECRET_PATTERN.search(json.dumps(value, ensure_ascii=False, default=str)):
        raise ValueError("Do not serialize credentials; record environment variable names only")


def _relative_path(value: str) -> str:
    path = PurePosixPath(value)
    if (
        not value
        or value != path.as_posix()
        or path.is_absolute()
        or "\\" in value
        or ":" in value
        or any(ord(char) < 32 for char in value)
        or any(part in {".", "..", ".git", ".env"} or part.startswith(".env.") for part in path.parts)
    ):
        raise ValueError("Use a normalized repository-relative non-secret path")
    return value


class Artifact(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, revalidate_instances="always")

    @model_validator(mode="after")
    def no_serialized_secrets(self):
        _reject_secrets(self.model_dump(mode="json"))
        return self


class Stage(StrEnum):
    UNDERSTAND = "understand"
    SELECT = "select"
    WIRE = "wire"
    REVIEW = "review"
    GENERATE = "generate"
    VERIFY = "verify"


class Wiring(Artifact):
    operation: Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]{0,99}$")]
    interface: Text
    source_path: str = Field(max_length=500)
    source_digest: Digest | None = None
    side_effects: Text

    _safe_source_path = field_validator("source_path")(_relative_path)


class WorkflowState(Artifact):
    schema_version: Literal[1] = 1
    workflow_id: WorkflowId
    plan_id: Annotated[str, Field(pattern=r"^[A-Za-z0-9_-]{1,100}$")]
    plan_digest: Digest
    repository_revision: Text
    revision: int = Field(ge=1, strict=True)
    stage: Stage
    summary: Text
    selections: tuple[Text, ...] = Field(default=(), max_length=100)
    wiring: tuple[Wiring, ...] = Field(default=(), max_length=100)
    review_notes: tuple[Text, ...] = Field(default=(), max_length=100)
    feedback: tuple[Text, ...] = Field(default=(), max_length=100)
    generation_digest: Digest | None = None

    @model_validator(mode="after")
    def stage_requirements(self):
        if self.stage in {Stage.SELECT, Stage.WIRE, Stage.REVIEW, Stage.GENERATE, Stage.VERIFY} and not self.selections:
            raise ValueError("Selection stage and later require at least one selected capability")
        if self.stage in {Stage.WIRE, Stage.REVIEW, Stage.GENERATE, Stage.VERIFY} and not self.wiring:
            raise ValueError("Wire stage and later require at least one interface mapping")
        if self.stage in {Stage.REVIEW, Stage.GENERATE, Stage.VERIFY} and not self.review_notes:
            raise ValueError("Review stage and later require review notes")
        if self.stage in {Stage.GENERATE, Stage.VERIFY} and self.generation_digest is None:
            raise ValueError("Generate stage and later require an exact generation digest")
        if self.stage not in {Stage.GENERATE, Stage.VERIFY} and self.generation_digest is not None:
            raise ValueError("A revised pre-generation state cannot retain generated output")
        return self


class Binding(Artifact):
    workflow_id: WorkflowId
    plan_id: Annotated[str, Field(pattern=r"^[A-Za-z0-9_-]{1,100}$")]
    plan_digest: Digest
    revision: int = Field(ge=1, strict=True)
    approved: Literal[True]
    reviewer: Text
    approved_at: datetime

    @field_validator("approved", mode="before")
    @classmethod
    def explicit_true(cls, value):
        if type(value) is not bool or value is not True:
            raise ValueError("Approval must be the explicit boolean true")
        return value

    @field_validator("reviewer")
    @classmethod
    def nonempty_reviewer(cls, value):
        value = value.strip()
        if not value:
            raise ValueError("Approval requires a reviewer identity")
        return value

    @field_validator("approved_at")
    @classmethod
    def auditable_time(cls, value):
        if value.tzinfo is None:
            raise ValueError("Approval timestamp requires timezone")
        if value > datetime.now(timezone.utc) + timedelta(minutes=5):
            raise ValueError("Approval timestamp cannot be in the future")
        return value


class GenerationApproval(Binding):
    kind: Literal["generation"] = "generation"


class GenerationOutput(Artifact):
    path: str = Field(max_length=500)
    content_digest: Digest
    purpose: Text

    _safe_path = field_validator("path")(_relative_path)


class GenerationManifest(Artifact):
    schema_version: Literal[1] = 1
    workflow_id: WorkflowId
    plan_id: Annotated[str, Field(pattern=r"^[A-Za-z0-9_-]{1,100}$")]
    plan_digest: Digest
    revision: int = Field(ge=1, strict=True)
    generator: Text
    outputs: tuple[GenerationOutput, ...] = Field(min_length=1, max_length=500)
    generated_at: datetime
    executed_by_workflow: Literal[False] = False

    @field_validator("generated_at")
    @classmethod
    def generated_time_has_timezone(cls, value):
        if value.tzinfo is None:
            raise ValueError("Generation timestamp requires timezone")
        return value

    @model_validator(mode="after")
    def unique_outputs(self):
        paths = [output.path for output in self.outputs]
        if len(paths) != len(set(paths)):
            raise ValueError("Generation output paths must be unique")
        return self


def generation_digest(manifest: GenerationManifest) -> str:
    validated = GenerationManifest.model_validate(manifest)
    return hashlib.sha256(_canonical(validated.model_dump(mode="json"))).hexdigest()


def _artifact_digest(artifact: Artifact) -> str:
    validated = type(artifact).model_validate(artifact)
    return hashlib.sha256(_canonical(validated.model_dump(mode="json"))).hexdigest()


class ApplyApproval(Binding):
    kind: Literal["apply"] = "apply"
    generation_digest: Digest


class VerificationObservation(Artifact):
    check: Annotated[str, Field(pattern=r"^[a-z][a-z0-9_.-]{0,199}$")]
    outcome: Literal["passed", "failed", "not_run"]
    evidence_path: str | None = Field(default=None, max_length=500)
    details: Text

    @field_validator("evidence_path")
    @classmethod
    def safe_evidence_path(cls, value):
        return _relative_path(value) if value is not None else None

    @model_validator(mode="after")
    def evidence_for_result(self):
        if self.outcome in {"passed", "failed"} and self.evidence_path is None:
            raise ValueError("Executed checks require repository-relative evidence")
        return self


class VerificationReport(Artifact):
    schema_version: Literal[1] = 1
    workflow_id: WorkflowId
    plan_id: Annotated[str, Field(pattern=r"^[A-Za-z0-9_-]{1,100}$")]
    plan_digest: Digest
    revision: int = Field(ge=1, strict=True)
    generation_digest: Digest
    environment: Literal["mock", "local", "staging", "production"]
    runner: Text
    observations: tuple[VerificationObservation, ...] = Field(min_length=1, max_length=500)
    observed_at: datetime
    executed_by_workflow: Literal[False] = False
    production_ready: Literal[False] = False

    @field_validator("observed_at")
    @classmethod
    def observation_time_has_timezone(cls, value):
        if value.tzinfo is None:
            raise ValueError("Verification timestamp requires timezone")
        return value


class Event(Artifact):
    sequence: int = Field(ge=1, strict=True)
    occurred_at: datetime
    action: Literal[
        "created",
        "transitioned",
        "feedback_recorded",
        "generation_approved",
        "generation_recorded",
        "apply_approved",
        "verification_recorded",
    ]
    previous_hash: Digest | Literal["genesis"]
    state: WorkflowState
    artifact_digest: Digest | None = None
    event_hash: Digest


def _event_hash(data: dict) -> str:
    unsigned = {key: value for key, value in data.items() if key != "event_hash"}
    return hashlib.sha256(_canonical(unsigned)).hexdigest()


class WorkflowStore:
    """Persist a workflow without generating, applying, or executing customer code."""

    _FORWARD = {
        Stage.UNDERSTAND: Stage.SELECT,
        Stage.SELECT: Stage.WIRE,
        Stage.WIRE: Stage.REVIEW,
    }
    _REVISABLE = {Stage.UNDERSTAND, Stage.SELECT, Stage.WIRE, Stage.REVIEW}

    def __init__(self, root: str | Path, workflow_id: str):
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,99}", workflow_id):
            raise ValueError("Invalid workflow id")
        self.root = Path(root).expanduser().resolve()
        self.workflow_id = workflow_id
        self.directory = self.root / workflow_id
        self._prepare_directory()

    def _prepare_directory(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        if self.directory.is_symlink():
            raise ValueError("Workflow directory must not be a symlink")
        self.directory.mkdir(mode=0o700, exist_ok=True)
        if self.directory.resolve().parent != self.root:
            raise ValueError("Workflow directory escaped its configured root")
        for name in ("approvals", "generations", "verification"):
            child = self.directory / name
            if child.is_symlink():
                raise ValueError("Workflow artifact directory must not be a symlink")
            child.mkdir(mode=0o700, exist_ok=True)

    @property
    def _events_path(self) -> Path:
        return self.directory / "events.ndjson"

    @property
    def _lock_path(self) -> Path:
        return self.directory / ".lock"

    def _locked(self):
        flags = os.O_RDWR | os.O_CREAT
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(self._lock_path, flags, 0o600)

        class Lock:
            def __enter__(inner):
                fcntl.flock(descriptor, fcntl.LOCK_EX)
                return inner

            def __exit__(inner, *_):
                fcntl.flock(descriptor, fcntl.LOCK_UN)
                os.close(descriptor)

        return Lock()

    def _load_events(self) -> list[Event]:
        if not self._events_path.exists():
            return []
        if self._events_path.is_symlink():
            raise ValueError("Event log must not be a symlink")
        events: list[Event] = []
        previous = "genesis"
        with self._events_path.open("rb") as stream:
            for index, raw in enumerate(stream, start=1):
                if not raw.endswith(b"\n"):
                    raise ValueError("Truncated event log")
                try:
                    data = json.loads(raw)
                except json.JSONDecodeError as error:
                    raise ValueError("Invalid event log JSON") from error
                event = Event.model_validate(data)
                if event.sequence != index or event.previous_hash != previous:
                    raise ValueError("Broken event sequence or hash chain")
                if _event_hash(data) != event.event_hash:
                    raise ValueError("Tampered event log")
                if event.state.workflow_id != self.workflow_id:
                    raise ValueError("Event belongs to another workflow")
                events.append(event)
                previous = event.event_hash
        return events

    def load(self) -> WorkflowState:
        events = self._load_events()
        if not events:
            raise FileNotFoundError(f"Workflow {self.workflow_id!r} does not exist")
        return events[-1].state

    def events(self) -> tuple[Event, ...]:
        return tuple(self._load_events())

    def _append_bytes(self, content: bytes) -> None:
        flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(self._events_path, flags, 0o600)
        try:
            written = os.write(descriptor, content)
            if written != len(content):
                raise OSError("Short append to workflow event log")
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def _append(self, action: str, state: WorkflowState, artifact_digest: str | None = None) -> Event:
        existing = self._load_events()
        data = {
            "sequence": len(existing) + 1,
            # Match Pydantic's JSON representation so the bytes hashed here are
            # exactly the semantic representation persisted below.
            "occurred_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "action": action,
            "previous_hash": existing[-1].event_hash if existing else "genesis",
            "state": state.model_dump(mode="json"),
            "artifact_digest": artifact_digest,
        }
        data["event_hash"] = _event_hash(data)
        event = Event.model_validate(data)
        self._append_bytes(_canonical(event.model_dump(mode="json")) + b"\n")
        return event

    def _write_new(self, path: Path, artifact: Artifact) -> None:
        if path.parent.resolve().parent != self.directory.resolve():
            raise ValueError("Artifact path escaped the workflow directory")
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(path, flags, 0o600)
        content = _canonical(artifact.model_dump(mode="json")) + b"\n"
        try:
            position = 0
            while position < len(content):
                position += os.write(descriptor, content[position:])
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def _write_and_record(
        self,
        path: Path,
        artifact: Artifact,
        *,
        action: str,
        state: WorkflowState,
        artifact_digest: str,
    ) -> None:
        """Publish an immutable artifact only when its event can be appended.

        The rollback handles failures before/during the append attempt. As with any
        local append-only file, an operating-system failure after bytes reach disk
        but before it reports success requires operator recovery.
        """
        self._write_new(path, artifact)
        try:
            self._append(action, state, artifact_digest)
        except BaseException:
            try:
                path.unlink()
            except OSError:
                pass
            raise

    def _require_persisted_approval(
        self,
        current: WorkflowState,
        approval: Binding,
        *,
        kind: Literal["generation", "apply"],
    ) -> None:
        self._binding(current, approval)
        path = self.directory / "approvals" / f"{kind}-r{current.revision}.json"
        try:
            stored_data = json.loads(path.read_bytes())
        except (FileNotFoundError, json.JSONDecodeError) as error:
            raise ValueError(f"A persisted {kind} approval is required") from error
        expected_type = GenerationApproval if kind == "generation" else ApplyApproval
        stored = expected_type.model_validate(stored_data)
        if _canonical(stored.model_dump(mode="json")) != _canonical(approval.model_dump(mode="json")):
            raise ValueError("Approval does not match the persisted approval artifact")
        digest = _artifact_digest(stored)
        if not any(
            event.action == f"{kind}_approved"
            and event.state.revision == current.revision
            and event.artifact_digest == digest
            for event in self._load_events()
        ):
            raise ValueError("Approval artifact is not bound to the append-only event log")

    def create(self, *, plan_id: str, plan_digest: str, repository_revision: str, summary: str) -> WorkflowState:
        with self._locked():
            if self._load_events():
                raise FileExistsError(f"Workflow {self.workflow_id!r} already exists")
            state = WorkflowState(
                workflow_id=self.workflow_id,
                plan_id=plan_id,
                plan_digest=plan_digest,
                repository_revision=repository_revision,
                revision=1,
                stage=Stage.UNDERSTAND,
                summary=summary,
            )
            self._append("created", state)
            return state

    def transition(
        self,
        stage: Stage,
        *,
        expected_revision: int,
        summary: str | None = None,
        selections: tuple[str, ...] | None = None,
        wiring: tuple[Wiring, ...] | None = None,
        review_notes: tuple[str, ...] | None = None,
    ) -> WorkflowState:
        with self._locked():
            current = self.load()
            if current.revision != expected_revision:
                raise ValueError("Stale workflow revision")
            if self._FORWARD.get(current.stage) != stage:
                raise ValueError(f"Invalid transition from {current.stage} to {stage}")
            state = WorkflowState.model_validate(
                {
                    **current.model_dump(),
                    "revision": current.revision + 1,
                    "stage": stage,
                    "summary": summary or current.summary,
                    "selections": selections if selections is not None else current.selections,
                    "wiring": wiring if wiring is not None else current.wiring,
                    "review_notes": review_notes if review_notes is not None else current.review_notes,
                    "generation_digest": None,
                }
            )
            self._append("transitioned", state)
            return state

    def feedback(self, target: Stage, note: str, *, expected_revision: int) -> WorkflowState:
        if target not in self._REVISABLE:
            raise ValueError("Feedback must return to understand, select, wire, or review")
        with self._locked():
            current = self.load()
            if current.revision != expected_revision:
                raise ValueError("Stale workflow revision")
            data = current.model_dump()
            data.update(
                revision=current.revision + 1,
                stage=target,
                feedback=(*current.feedback, note),
                generation_digest=None,
            )
            if target == Stage.UNDERSTAND:
                data.update(selections=(), wiring=(), review_notes=())
            elif target == Stage.SELECT:
                data.update(wiring=(), review_notes=())
            elif target == Stage.WIRE:
                data.update(review_notes=())
            state = WorkflowState.model_validate(data)
            self._append("feedback_recorded", state)
            return state

    def _binding(self, current: WorkflowState, approval: Binding) -> None:
        approval = type(approval).model_validate(approval)
        expected = (
            current.workflow_id,
            current.plan_id,
            current.plan_digest,
            current.revision,
        )
        actual = (approval.workflow_id, approval.plan_id, approval.plan_digest, approval.revision)
        if actual != expected:
            raise ValueError("Stale or tampered approval: exact workflow binding required")

    def approve_generation(
        self,
        *,
        expected_revision: int,
        approved: bool,
        reviewer: str,
        approved_at: datetime,
    ) -> GenerationApproval:
        with self._locked():
            current = self.load()
            if current.revision != expected_revision or current.stage != Stage.REVIEW:
                raise ValueError("Generation can only be approved for the current review revision")
            approval = GenerationApproval(
                workflow_id=current.workflow_id,
                plan_id=current.plan_id,
                plan_digest=current.plan_digest,
                revision=current.revision,
                approved=approved,
                reviewer=reviewer,
                approved_at=approved_at,
            )
            self._write_and_record(
                self.directory / "approvals" / f"generation-r{current.revision}.json",
                approval,
                action="generation_approved",
                state=current,
                artifact_digest=_artifact_digest(approval),
            )
            return approval

    def record_generation(
        self,
        approval: GenerationApproval,
        *,
        expected_revision: int,
        generator: str,
        outputs: tuple[GenerationOutput, ...],
        generated_at: datetime,
    ) -> GenerationManifest:
        with self._locked():
            current = self.load()
            if current.stage != Stage.REVIEW or current.revision != expected_revision:
                raise ValueError("Generation record requires the current review revision")
            self._require_persisted_approval(current, approval, kind="generation")
            manifest = GenerationManifest(
                workflow_id=current.workflow_id,
                plan_id=current.plan_id,
                plan_digest=current.plan_digest,
                revision=current.revision,
                generator=generator,
                outputs=outputs,
                generated_at=generated_at,
            )
            digest = generation_digest(manifest)
            generated = WorkflowState.model_validate(
                {**current.model_dump(), "stage": Stage.GENERATE, "generation_digest": digest}
            )
            self._write_and_record(
                self.directory / "generations" / f"generation-r{current.revision}.json",
                manifest,
                action="generation_recorded",
                state=generated,
                artifact_digest=digest,
            )
            return manifest

    def approve_apply(
        self,
        *,
        expected_revision: int,
        approved: bool,
        reviewer: str,
        approved_at: datetime,
    ) -> ApplyApproval:
        with self._locked():
            current = self.load()
            if current.revision != expected_revision or current.stage != Stage.GENERATE:
                raise ValueError("Apply can only be approved for current generated output")
            manifest_path = self.directory / "generations" / f"generation-r{current.revision}.json"
            try:
                manifest = GenerationManifest.model_validate_json(manifest_path.read_bytes())
            except (FileNotFoundError, ValueError) as error:
                raise ValueError("Current generation manifest is missing or invalid") from error
            if generation_digest(manifest) != current.generation_digest:
                raise ValueError("Current generation manifest was tampered with")
            approval = ApplyApproval(
                workflow_id=current.workflow_id,
                plan_id=current.plan_id,
                plan_digest=current.plan_digest,
                revision=current.revision,
                approved=approved,
                reviewer=reviewer,
                approved_at=approved_at,
                generation_digest=current.generation_digest,
            )
            self._write_and_record(
                self.directory / "approvals" / f"apply-r{current.revision}.json",
                approval,
                action="apply_approved",
                state=current,
                artifact_digest=_artifact_digest(approval),
            )
            return approval

    def record_verification(
        self,
        approval: ApplyApproval,
        *,
        expected_revision: int,
        environment: Literal["mock", "local", "staging", "production"],
        runner: str,
        observations: tuple[VerificationObservation, ...],
        observed_at: datetime,
    ) -> VerificationReport:
        with self._locked():
            current = self.load()
            if current.stage != Stage.GENERATE or current.revision != expected_revision:
                raise ValueError("Verification record requires current generated output")
            self._require_persisted_approval(current, approval, kind="apply")
            if approval.generation_digest != current.generation_digest:
                raise ValueError("Apply approval does not match current generated output")
            report = VerificationReport(
                workflow_id=current.workflow_id,
                plan_id=current.plan_id,
                plan_digest=current.plan_digest,
                revision=current.revision,
                generation_digest=current.generation_digest,
                environment=environment,
                runner=runner,
                observations=observations,
                observed_at=observed_at,
            )
            digest = hashlib.sha256(_canonical(report.model_dump(mode="json"))).hexdigest()
            verified = WorkflowState.model_validate({**current.model_dump(), "stage": Stage.VERIFY})
            self._write_and_record(
                self.directory / "verification" / f"verification-r{current.revision}.json",
                report,
                action="verification_recorded",
                state=verified,
                artifact_digest=digest,
            )
            return report
