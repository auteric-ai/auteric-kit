"""Distribution metadata for the local Auteric coding-agent kit."""

from __future__ import annotations

import io
import os
import re
import zipfile
from pathlib import Path
from urllib.parse import quote, urlsplit

from fastapi.responses import Response


_MODULE_PATH = Path(__file__).resolve()
_CONTAINER_KIT_ROOT = _MODULE_PATH.parent / "kits" / "auteric-kit"
_SOURCE_KIT_ROOT = _MODULE_PATH.parents[2] / "kits" / "auteric-kit" if len(_MODULE_PATH.parents) > 2 else _CONTAINER_KIT_ROOT
KIT_ROOT = _CONTAINER_KIT_ROOT if _CONTAINER_KIT_ROOT.is_dir() else _SOURCE_KIT_ROOT


def _repository():
    """Use the published kit repository, with an explicit override for other builds."""
    slug = os.getenv("AUTERIC_KIT_REPOSITORY", "auteric-ai/auteric-kit").strip()
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", slug):
        return None
    return slug


def _https_url(value: str | None) -> str | None:
    """Accept only an explicitly configured public Cloud endpoint."""
    candidate = (value or "").strip()
    parsed = urlsplit(candidate)
    if parsed.scheme == "https" and parsed.netloc and not parsed.username and not parsed.password:
        return candidate
    return None


def kit_context(base_url: str, domain: str) -> dict:
    slug = _repository()
    repo_url = "https://github.com/" + slug if slug else None
    source = slug if slug else "./auteric-kit"
    # Link to the published repository; never invent a star count.
    commands = {
        "codex": f"codex plugin marketplace add {source} && codex plugin add auteric-kit",
        "claude": f"claude plugin marketplace add {source} && claude plugin install auteric-kit@auteric",
        "cursor": f"npx skills add {source}/plugins/auteric-kit --skill '*' --agent cursor",
        "any": f"npx skills add {source}/plugins/auteric-kit",
    }
    # Native agent actions are primary where the agent supports them. The CLI
    # remains the fallback for generic environments only.
    invocations = {
        "codex": "$auteric-connect",
        "claude": "/auteric-kit:connect",
        "cursor": "/auteric-connect",
        "any": f"auteric connect --domain {domain}",
    }
    agents = {}
    for agent, label in [("codex", "Codex"), ("claude", "Claude Code"), ("cursor", "Cursor"), ("any", "Other coding agent")]:
        # The detailed workflow lives in the installed, versioned skill. Scanner
        # only needs to provide an installation command and short native trigger.
        invocation = invocations[agent]
        prompt = invocation
        if agent == "codex":
            launch_url = "codex://new?prompt=" + quote(prompt, safe="")
        elif agent == "claude":
            launch_url = "claude-cli://open?q=" + quote(prompt, safe="")
        elif agent == "cursor":
            launch_url = "https://cursor.com/link/prompt?text=" + quote(prompt, safe="")
        else:
            launch_url = None
        agents[agent] = {"label": label, "command": commands[agent], "invocation": invocation,
                         "prompt": prompt, "launch_url": launch_url}
    # A builder selection must change the next useful action.  These steps do
    # not claim that Scanner has access to a builder or its repository; that is
    # only true after the future GitHub authorization flow succeeds.
    builders = {
        "lovable": {
            "label": "Lovable",
            "title": "Find the source behind this Lovable project.",
            "description": "Open the Lovable project that publishes this store, then identify its linked GitHub repository.",
            "steps": [
                "Open the project that publishes this store in Lovable.",
                "Open its project settings and copy the linked GitHub repository URL.",
                "Run the public store check below now; it checks the published store without reading the repository.",
            ],
        },
        "base44": {
            "label": "Base44",
            "title": "Locate or export the Base44 store source.",
            "description": "Use the Base44 project that deploys this store and bring its source under a GitHub repository when one is available.",
            "steps": [
                "Open the Base44 project that publishes this store.",
                "Use its source-control or export option when available, then copy the GitHub repository URL.",
                "Run the public store check below now; it checks the published store without reading the repository.",
            ],
        },
        "replit": {
            "label": "Replit",
            "title": "Connect the Replit project source.",
            "description": "Open the Replit app that publishes this store and use its version-control connection to identify the GitHub repository.",
            "steps": [
                "Open the Replit app that publishes this store.",
                "Open Version control, connect or select the GitHub repository, and copy its URL.",
                "Run the public store check below now; it checks the published store without reading the repository.",
            ],
        },
        "other": {
            "label": "Other website builder",
            "title": "Put the deployed store source in one reviewable repository.",
            "description": "Find the project that deploys this store, then use its GitHub sync or code-export option.",
            "steps": [
                "Open the builder project that publishes this store.",
                "Copy its GitHub repository URL, or export the code and create one repository for it.",
                "Run the public store check below now; it checks the published store without reading the repository.",
            ],
        },
    }
    github_url = _https_url(os.getenv("AUTERIC_GITHUB_ONBOARDING_URL"))
    return {"repo_url": repo_url, "download_url": base_url.rstrip("/") + "/downloads/auteric-kit.zip",
            "agents": agents, "builders": builders, "github_onboarding_url": github_url,
            "github_onboarding_available": bool(github_url)}


def install_kit_routes(app) -> None:
    @app.get("/downloads/auteric-kit.zip", include_in_schema=False)
    def download_auteric_kit():
        if not KIT_ROOT.is_dir():
            return Response(status_code=503)
        payload = io.BytesIO()
        with zipfile.ZipFile(payload, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for path in sorted(KIT_ROOT.rglob("*")):
                if path.is_file() and not any(part in {"__pycache__", ".pytest_cache", ".git", "dist"} for part in path.relative_to(KIT_ROOT).parts):
                    archive.write(path, "auteric-kit/" + path.relative_to(KIT_ROOT).as_posix())
        return Response(payload.getvalue(), media_type="application/zip", headers={
            "Content-Disposition": 'attachment; filename="auteric-kit.zip"',
            "Cache-Control": "public, max-age=300",
            "X-Content-Type-Options": "nosniff",
        })
