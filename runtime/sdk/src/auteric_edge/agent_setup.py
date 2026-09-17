"""Install instruction files locally; never analyze/upload code or call an LLM."""

import json
from importlib.resources import files
from pathlib import Path


def instructions():
    return files("auteric_edge").joinpath("agent_skill/SKILL.md").read_text(encoding="utf-8")


def install(client, destination):
    """Explicit instruction installation, not authority for merchant code changes.

    A chosen target file is never overwritten. Intermediate symlinks are refused.
    Destination is explicit so installation never edits global assistant settings.
    """
    relative = {
        "codex": ".agents/skills/auteric-commerce/SKILL.md",
        "claude-code": ".claude/skills/auteric-commerce/SKILL.md",
        "cursor": ".cursor/rules/auteric-commerce.mdc",
    }
    if client not in relative:
        raise ValueError("Choose codex, claude-code or cursor")
    root = Path(destination).absolute()
    if not root.is_dir() or root.is_symlink():
        raise ValueError("Choose an existing merchant repository directory")
    target = root / relative[client]
    for parent in [root, *target.parents]:
        if parent.is_symlink():
            raise ValueError("Refusing symlinked installation path")
        if parent == root.parent:
            break
    content = instructions()
    if client == "cursor":
        body = content.split("---", 2)[2].lstrip()
        content = (
            "---\ndescription: Connect existing commerce code to Auteric with developer review\n"
            "alwaysApply: false\n---\n\n" + body
        )
    if target.exists():
        if target.is_file() and not target.is_symlink() and target.read_text(encoding="utf-8") == content:
            return {"path": str(target), "changed": False}
        raise FileExistsError("Instruction file already exists and differs; review manually, no overwrite")
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("x", encoding="utf-8") as stream:
        stream.write(content)
    return {"path": str(target), "changed": True}


def package(destination):
    """Build a self-contained local plugin, without installing or publishing it."""
    parent = Path(destination).absolute()
    if not parent.is_dir() or any(p.is_symlink() for p in (parent, *parent.parents)):
        raise ValueError("Choose an existing non-symlink output directory")
    root = parent / "auteric-commerce"
    # Exclusive creation: no partial overwrites of an existing plugin.
    root.mkdir()
    manifest = {
        "name": "auteric-commerce",
        "version": "0.1.0",
        "description": "Reviewed deterministic commerce integrations and verification for Auteric.",
        "author": {"name": "Auteric Security"},
        "skills": "./skills/",
    }
    codex = {
        **manifest,
        "interface": {
            "displayName": "Auteric Commerce",
            "shortDescription": "Connect commerce with evidence and review",
            "longDescription": "Inspect merchant code, review a plan, build connectors and verify actual behavior.",
            "developerName": "Auteric Security",
            "category": "Productivity",
            "capabilities": ["Write"],
            "defaultPrompt": [
                "Inspect this merchant repository and propose an Auteric integration plan without editing code."
            ],
        },
    }
    for folder, data in ((".codex-plugin", codex), (".claude-plugin", manifest)):
        path = root / folder
        path.mkdir()
        (path / "plugin.json").write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    for name, content in (
        ("auteric-commerce", instructions()),
        ("auteric-verify", files("auteric_edge").joinpath("agent_skill/VERIFY.md").read_text(encoding="utf-8")),
        ("auteric-webmcp", files("auteric_edge").joinpath("agent_skill/WEBMCP.md").read_text(encoding="utf-8")),
    ):
        path = root / "skills" / name
        path.mkdir(parents=True)
        (path / "SKILL.md").write_text(content, encoding="utf-8")
    return {"path": str(root), "installed": False, "published": False}
