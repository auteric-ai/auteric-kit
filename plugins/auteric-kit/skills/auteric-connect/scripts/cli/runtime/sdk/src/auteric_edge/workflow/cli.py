"""Local CLI for the Explorer-to-workflow bridge; it never applies source."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .explorer_bridge import bridge_explorer_review, generate_bound_candidate


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m auteric_edge.workflow.cli")
    commands = parser.add_subparsers(dest="command", required=True)

    bridge = commands.add_parser("bridge", help="Create a review workflow from an exact approved Explorer state")
    bridge.add_argument("--project-root", required=True)
    bridge.add_argument("--workflow-id", required=True)
    bridge.add_argument("--binding-map", required=True)
    bridge.add_argument("--repository-revision", required=True)

    generate = commands.add_parser("generate", help="Generate an immutable review-only first-party binding candidate")
    generate.add_argument("--project-root", required=True)
    generate.add_argument("--workflow-id", required=True)
    generate.add_argument("--binding-map", required=True)
    generate.add_argument("--confirm-review-digest", required=True)
    generate.add_argument("--reviewer", required=True)
    generate.add_argument(
        "--approve-generation",
        action="store_true",
        help="Explicitly approve candidate generation; this does NOT approve applying code",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "bridge":
            state = bridge_explorer_review(
                args.project_root,
                workflow_id=args.workflow_id,
                binding_map=args.binding_map,
                repository_revision=args.repository_revision,
            )
            result = {
                "workflow_id": state.workflow_id,
                "workflow_revision": state.revision,
                "stage": state.stage,
                "review_digest": state.plan_digest,
                "apply_approved": False,
            }
        else:
            if not args.approve_generation:
                parser.error("generate requires --approve-generation; apply remains unapproved")
            candidate, manifest = generate_bound_candidate(
                args.project_root,
                workflow_id=args.workflow_id,
                binding_map=args.binding_map,
                confirmed_review_digest=args.confirm_review_digest,
                reviewer=args.reviewer,
                approved=True,
            )
            result = {
                "candidate": str(candidate),
                "manifest": manifest.model_dump(mode="json"),
                "apply_approved": False,
                "executed": False,
            }
    except (FileExistsError, FileNotFoundError, ValueError) as error:
        parser.exit(1, f"Failed safely: {error}\n")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
