"""Bridge an approved local Explorer review into deterministic workflow state.

This module reads only the fixed ``.auteric/explorer-state.json`` file beneath an
explicit project root. It neither executes discovered snippets nor applies generated
source to the merchant repository.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .store import (
    GenerationApproval,
    GenerationManifest,
    GenerationOutput,
    Stage,
    Wiring,
    WorkflowState,
    WorkflowStore,
)

_DIGEST = re.compile(r"^[a-f0-9]{64}$")
_IDENTIFIER = re.compile(r"^[A-Za-z_$][A-Za-z0-9_$]*$")
_OPERATION = re.compile(r"^[a-z][a-z0-9_]{0,99}$")
_MODULE_SUFFIXES = {".js", ".mjs", ".cjs", ".ts", ".tsx"}
_MAX_STATE_BYTES = 1_000_000
_MAX_MAPPING_BYTES = 256_000
_MAX_MODULE_BYTES = 2_000_000


def _canonical(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _relative_path(value: str) -> str:
    candidate = PurePosixPath(value)
    if (
        not value
        or candidate.is_absolute()
        or candidate.as_posix() != value
        or "\\" in value
        or ":" in value
        or any(part in {".", "..", ".git", ".env", ".auteric"} or part.startswith(".env.") for part in candidate.parts)
    ):
        raise ValueError("modulePath must be a normalized first-party repository path")
    if candidate.suffix not in _MODULE_SUFFIXES:
        raise ValueError("modulePath must name a JavaScript or TypeScript module")
    return value


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ExplorerEvidence(_Strict):
    kind: Literal["dom", "network", "manifest", "operator", "test"]
    source: str = Field(min_length=1, max_length=500)
    observedAt: datetime
    detail: str = Field(max_length=1000)

    @field_validator("observedAt")
    @classmethod
    def observed_time_has_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("Explorer evidence timestamp requires timezone")
        return value


class ExplorerOrigin(_Strict):
    kind: Literal["browser_discovery"]
    identity: str = Field(min_length=1, max_length=64)
    candidateId: str = Field(min_length=1, max_length=200)
    classification: str = Field(min_length=1, max_length=160)
    sourceUrl: str = Field(min_length=1, max_length=2000)
    targetUrl: str | None = Field(default=None, max_length=2000)
    reportUrl: str = Field(min_length=1, max_length=2000)
    reportFingerprint: str

    @field_validator("reportFingerprint")
    @classmethod
    def valid_digest(cls, value: str) -> str:
        if not _DIGEST.fullmatch(value):
            raise ValueError("Invalid report fingerprint")
        return value


class ExplorerRoute(_Strict):
    id: str = Field(min_length=1, max_length=64)
    path: str = Field(min_length=1, max_length=500)
    label: str = Field(min_length=1, max_length=200)
    selected: bool
    sourceUrl: str | None = Field(default=None, max_length=2000)
    importedIdentity: str | None = Field(default=None, max_length=64)

    @field_validator("path")
    @classmethod
    def application_route(cls, value: str) -> str:
        if not value.startswith("/") or value.startswith("//"):
            raise ValueError("Explorer route must be application-relative")
        return value


class ExplorerTool(_Strict):
    id: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=160)
    description: str = Field(min_length=1, max_length=1000)
    routeId: str | None
    method: Literal["GET", "POST", "PUT", "PATCH", "DELETE"]
    disposition: Literal["selected", "dropped"]
    sensitivity: Literal["none", "sensitive"]
    consequence: Literal["read_only", "navigation", "external", "write"] = "read_only"
    reviewStatus: Literal["suggested"] = "suggested"
    executionVerified: Literal[False] = False
    evidence: tuple[ExplorerEvidence, ...] = Field(max_length=500)
    origin: ExplorerOrigin | None = None

    @field_validator("name")
    @classmethod
    def canonical_operation(cls, value: str) -> str:
        if not _OPERATION.fullmatch(value):
            raise ValueError("Selected Explorer tool names must be canonical operation identifiers")
        return value


class ExplorerNote(_Strict):
    id: str = Field(min_length=1, max_length=64)
    targetId: str = Field(min_length=1, max_length=64)
    text: str = Field(min_length=1, max_length=2000)
    createdAt: datetime


class ExplorerFeedback(_Strict):
    id: str = Field(min_length=1, max_length=64)
    summary: str = Field(min_length=1, max_length=2000)
    createdAt: datetime


class ExplorerApproval(_Strict):
    revision: int = Field(ge=0, strict=True)
    digest: str
    reviewer: str = Field(min_length=1, max_length=200)
    approvedAt: datetime

    @field_validator("reviewer")
    @classmethod
    def reviewer_is_not_whitespace(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Explorer approval requires a reviewer")
        return value

    @field_validator("approvedAt")
    @classmethod
    def approval_time_has_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("Explorer approval timestamp requires timezone")
        return value


class ExplorerVerification(_Strict):
    status: Literal["not_run", "running", "passed", "failed", "blocked"]
    summary: str = Field(max_length=2000)
    checkedAt: datetime | None


class ExplorerState(_Strict):
    schemaVersion: Literal[1]
    revision: int = Field(ge=0, strict=True)
    updatedAt: datetime
    routes: tuple[ExplorerRoute, ...] = Field(max_length=500)
    tools: tuple[ExplorerTool, ...] = Field(max_length=500)
    notes: tuple[ExplorerNote, ...] = Field(max_length=500)
    feedbackRounds: tuple[ExplorerFeedback, ...] = Field(max_length=500)
    approval: ExplorerApproval
    verification: ExplorerVerification
    digest: str

    @field_validator("updatedAt")
    @classmethod
    def updated_time_has_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("Explorer update timestamp requires timezone")
        return value

    @model_validator(mode="after")
    def references_and_approval(self):
        route_ids = {route.id for route in self.routes}
        tool_ids = {tool.id for tool in self.tools}
        if len(route_ids) != len(self.routes) or len(tool_ids) != len(self.tools):
            raise ValueError("Explorer ids must be unique")
        if any(tool.routeId is not None and tool.routeId not in route_ids for tool in self.tools):
            raise ValueError("Explorer tool references an unknown route")
        if any(note.targetId not in route_ids | tool_ids for note in self.notes):
            raise ValueError("Explorer note references an unknown target")
        if self.approval.revision != self.revision or self.approval.digest != self.digest:
            raise ValueError("Explorer approval is stale")
        return self


class FirstPartyBinding(_Strict):
    toolId: str = Field(min_length=1, max_length=64)
    modulePath: str = Field(max_length=500)
    exportName: str = Field(max_length=160)

    _safe_module_path = field_validator("modulePath")(_relative_path)

    @field_validator("exportName")
    @classmethod
    def valid_export(cls, value: str) -> str:
        if not _IDENTIFIER.fullmatch(value):
            raise ValueError("exportName must be a JavaScript identifier")
        return value


class BindingMap(_Strict):
    schemaVersion: Literal[1]
    bindings: tuple[FirstPartyBinding, ...] = Field(min_length=1, max_length=500)

    @model_validator(mode="after")
    def unique_tools(self):
        ids = [binding.toolId for binding in self.bindings]
        if len(ids) != len(set(ids)):
            raise ValueError("Each toolId may be bound only once")
        return self


def _fixed_json(path: Path, *, maximum: int, label: str) -> dict:
    try:
        info = path.lstat()
    except FileNotFoundError as error:
        raise ValueError(f"{label} does not exist") from error
    if path.is_symlink() or not stat.S_ISREG(info.st_mode):
        raise ValueError(f"{label} must be a regular non-symlink file")
    if info.st_size > maximum:
        raise ValueError(f"{label} is too large")
    try:
        data = json.loads(path.read_bytes())
    except json.JSONDecodeError as error:
        raise ValueError(f"{label} is not valid JSON") from error
    if not isinstance(data, dict):
        raise ValueError(f"{label} must contain a JSON object")
    return data


def _auteric_root(project_root: Path) -> Path:
    directory = project_root / ".auteric"
    try:
        info = directory.lstat()
    except FileNotFoundError as error:
        raise ValueError("Project has no .auteric review directory") from error
    if directory.is_symlink() or not stat.S_ISDIR(info.st_mode):
        raise ValueError("Project .auteric must be a real directory")
    return directory


def _safe_child_directory(parent: Path, name: str) -> Path:
    child = parent / name
    if child.exists() or child.is_symlink():
        info = child.lstat()
        if child.is_symlink() or not stat.S_ISDIR(info.st_mode):
            raise ValueError(f".auteric/{name} must be a real directory")
    else:
        child.mkdir(mode=0o700)
    return child


def load_approved_explorer_state(project_root: str | Path) -> ExplorerState:
    root = Path(project_root).expanduser().resolve()
    state_path = _auteric_root(root) / "explorer-state.json"
    raw = _fixed_json(state_path, maximum=_MAX_STATE_BYTES, label="Explorer state")
    basis = {key: raw.get(key) for key in ("schemaVersion", "revision", "routes", "tools", "notes", "feedbackRounds")}
    calculated = _sha256(_canonical(basis))
    if raw.get("digest") != calculated:
        raise ValueError("Explorer review digest does not match its authoritative basis")
    state = ExplorerState.model_validate(raw)
    if not _DIGEST.fullmatch(state.digest):
        raise ValueError("Explorer digest is invalid")
    return state


def load_binding_map(filename: str | Path) -> BindingMap:
    return BindingMap.model_validate(
        _fixed_json(Path(filename).expanduser().absolute(), maximum=_MAX_MAPPING_BYTES, label="Binding map")
    )


def _module_content(project_root: Path, binding: FirstPartyBinding) -> bytes:
    module = project_root / binding.modulePath
    try:
        info = module.lstat()
    except FileNotFoundError as error:
        raise ValueError(f"Mapped module does not exist: {binding.modulePath}") from error
    if module.is_symlink() or not stat.S_ISREG(info.st_mode) or module.resolve().is_relative_to(project_root) is False:
        raise ValueError(f"Mapped module must be a regular first-party file: {binding.modulePath}")
    if info.st_size > _MAX_MODULE_BYTES:
        raise ValueError(f"Mapped module is too large: {binding.modulePath}")
    content = module.read_bytes()
    try:
        source = content.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ValueError(f"Mapped module must be UTF-8 source: {binding.modulePath}") from error
    name = re.escape(binding.exportName)
    patterns = (
        rf"(?m)^\s*export\s+(?:async\s+)?(?:function|class|const|let|var)\s+{name}\b",
        rf"(?m)^\s*export\s*\{{[^}}]*\b{name}\b[^}}]*\}}",
        rf"(?m)^\s*exports\.{name}\s*=",
        rf"(?m)^\s*module\.exports\s*=\s*\{{[^}}]*\b{name}\b",
    )
    if not any(re.search(pattern, source) for pattern in patterns):
        raise ValueError(f"Mapped export was not found in first-party source: {binding.exportName}")
    return content


def _selected_tools(state: ExplorerState) -> tuple[ExplorerTool, ...]:
    selected_routes = {route.id for route in state.routes if route.selected}
    return tuple(
        tool
        for tool in state.tools
        if tool.disposition == "selected" and (tool.routeId is None or tool.routeId in selected_routes)
    )


def validate_bindings(project_root: str | Path, state: ExplorerState, mapping: BindingMap) -> dict[str, tuple[FirstPartyBinding, str]]:
    root = Path(project_root).expanduser().resolve()
    selected = _selected_tools(state)
    selected_ids = {tool.id for tool in selected}
    mapped_ids = {binding.toolId for binding in mapping.bindings}
    if not selected:
        raise ValueError("Explorer review has no selected tools")
    if mapped_ids != selected_ids:
        missing = sorted(selected_ids - mapped_ids)
        extra = sorted(mapped_ids - selected_ids)
        raise ValueError(f"Bindings must exactly cover selected tools; missing={missing}, extra={extra}")
    validated = {}
    for binding in mapping.bindings:
        content = _module_content(root, binding)
        validated[binding.toolId] = (binding, _sha256(content))
    return validated


def bridge_explorer_review(
    project_root: str | Path,
    *,
    workflow_id: str,
    binding_map: str | Path,
    repository_revision: str,
) -> WorkflowState:
    root = Path(project_root).expanduser().resolve()
    explorer = load_approved_explorer_state(root)
    mapping = load_binding_map(binding_map)
    validated = validate_bindings(root, explorer, mapping)
    selected = _selected_tools(explorer)
    workflow_root = _safe_child_directory(_auteric_root(root), "workflows")
    store = WorkflowStore(workflow_root, workflow_id)
    state = store.create(
        plan_id=f"explorer-r{explorer.revision}",
        plan_digest=explorer.digest,
        repository_revision=repository_revision,
        summary="Bridge the exact approved Explorer review to explicit first-party exports",
    )
    state = store.transition(Stage.SELECT, expected_revision=state.revision, selections=tuple(tool.id for tool in selected))
    by_id = {tool.id: tool for tool in selected}
    state = store.transition(
        Stage.WIRE,
        expected_revision=state.revision,
        wiring=tuple(
            Wiring(
                operation=by_id[binding.toolId].name,
                interface=f"{binding.modulePath}#{binding.exportName}",
                source_path=binding.modulePath,
                source_digest=validated[binding.toolId][1],
                side_effects=f"Explorer classification: {by_id[binding.toolId].consequence}; sensitivity: {by_id[binding.toolId].sensitivity}",
            )
            for binding in mapping.bindings
        ),
    )
    return store.transition(
        Stage.REVIEW,
        expected_revision=state.revision,
        review_notes=(
            f"Explorer revision {explorer.revision} approved by {explorer.approval.reviewer} at {explorer.approval.approvedAt.isoformat()}",
            "Mappings name existing first-party module exports; no merchant API was inferred",
        ),
    )


def _candidate_source(project_root: Path, directory: Path, explorer: ExplorerState, mapping: BindingMap) -> bytes:
    tools = {tool.id: tool for tool in _selected_tools(explorer)}
    lines = [
        "/* Auteric deterministic code candidate. REVIEW BEFORE APPLYING. */",
        f"/* Explorer revision {explorer.revision}; digest {explorer.digest}. */",
    ]
    for index, binding in enumerate(mapping.bindings):
        relative = os.path.relpath(project_root / binding.modulePath, directory).replace(os.sep, "/")
        if not relative.startswith("."):
            relative = f"./{relative}"
        lines.append(f"import {{ {binding.exportName} as handler{index} }} from {json.dumps(relative, ensure_ascii=False)};")
    lines.extend(
        [
            "",
            "export const autericReviewBinding = Object.freeze(" + json.dumps(
                {"revision": explorer.revision, "digest": explorer.digest}, ensure_ascii=False, separators=(",", ":"), sort_keys=True
            ) + ");",
            "",
            "export const autericToolCandidates = Object.freeze([",
        ]
    )
    for index, binding in enumerate(mapping.bindings):
        tool = tools[binding.toolId]
        metadata = {
            "consequence": tool.consequence,
            "description": tool.description,
            "executionVerified": False,
            "name": tool.name,
            "sensitivity": tool.sensitivity,
            "stableKey": tool.id,
            "status": "suggested",
        }
        lines.append(f"  Object.freeze({json.dumps(metadata, ensure_ascii=False, separators=(',', ':'), sort_keys=True)[:-1]},\"handler\": handler{index}}}),")
    lines.extend(["]);", ""])
    return "\n".join(lines).encode()


def generate_bound_candidate(
    project_root: str | Path,
    *,
    workflow_id: str,
    binding_map: str | Path,
    confirmed_review_digest: str,
    reviewer: str,
    approved: bool,
    generated_at: datetime | None = None,
) -> tuple[Path, GenerationManifest]:
    if type(approved) is not bool or approved is not True:
        raise ValueError("Candidate generation requires explicit approval")
    root = Path(project_root).expanduser().resolve()
    explorer = load_approved_explorer_state(root)
    if confirmed_review_digest != explorer.digest:
        raise ValueError("Confirmed review digest is stale or mistyped")
    mapping = load_binding_map(binding_map)
    validated = validate_bindings(root, explorer, mapping)
    auteric_root = _auteric_root(root)
    workflow_root = _safe_child_directory(auteric_root, "workflows")
    store = WorkflowStore(workflow_root, workflow_id)
    state = store.load()
    if state.stage != Stage.REVIEW or state.plan_digest != explorer.digest or state.plan_id != f"explorer-r{explorer.revision}":
        raise ValueError("Workflow is not bound to the current approved Explorer review")
    expected_wiring = {(wire.source_path, wire.interface, wire.source_digest) for wire in state.wiring}
    actual_wiring = {
        (binding.modulePath, f"{binding.modulePath}#{binding.exportName}", digest)
        for binding, digest in validated.values()
    }
    if actual_wiring != expected_wiring:
        raise ValueError("First-party binding or module content changed after workflow review")

    candidate_root = _safe_child_directory(auteric_root, "candidates")
    candidate_dir = candidate_root / f"{workflow_id}-r{state.revision}"
    if candidate_dir.exists() or candidate_dir.is_symlink():
        raise FileExistsError("Candidate directory already exists; generation is immutable")
    candidate_dir.mkdir(parents=True, mode=0o700)
    candidate_path = candidate_dir / "candidates.mjs"
    content = _candidate_source(root, candidate_dir, explorer, mapping)
    try:
        descriptor = os.open(candidate_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            offset = 0
            while offset < len(content):
                offset += os.write(descriptor, content[offset:])
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        approval = store.approve_generation(
            expected_revision=state.revision,
            approved=approved,
            reviewer=reviewer,
            approved_at=datetime.now(timezone.utc),
        )
        output_path = candidate_path.relative_to(root).as_posix()
        manifest = store.record_generation(
            approval,
            expected_revision=state.revision,
            generator="auteric deterministic first-party binding generator v1",
            outputs=(GenerationOutput(path=output_path, content_digest=_sha256(content), purpose="Review-only tool binding candidate"),),
            generated_at=generated_at or datetime.now(timezone.utc),
        )
        return candidate_path, manifest
    except BaseException:
        try:
            candidate_path.unlink(missing_ok=True)
            candidate_dir.rmdir()
        except OSError:
            pass
        raise
