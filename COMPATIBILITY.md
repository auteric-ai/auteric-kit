# Supported coding agents

Auteric Kit uses one shared integration workflow across supported coding agents. The wording and completion states are identical: inspect → plan → approval → implement → verify.

The source repository also contains an unpublished CLI preview with `--localhost`
for the local Commerce API. It performs shared browser authorization, pending
store registration and local UCP checks independently of the coding agent. Agent installation and
automatic invocation are not yet implemented by that CLI.

| Agent | How the merchant starts | What happens next |
| --- | --- | --- |
| Codex | Choose **Prepare this storefront for shopping agents with Auteric** from the plugin starter actions, or ask naturally. | The shared storefront workflow starts. |
| Claude Code | Run `/auteric-kit:prepare-storefront`, or ask naturally to prepare the store for shopping agents. | The command supplies the same user-facing workflow; natural requests activate the shared skill. |
| Cursor | Ask naturally to prepare the store for shopping agents after installing the skills. | Cursor discovers the shared skill from its installed skills directory. |
| Other Skills-compatible agents | Ask naturally to prepare the storefront for shopping agents. | The shared skill description supplies the same trigger and workflow. |

The hosts do not expose identical interface controls: Codex has starter prompts, Claude Code has plugin commands, and Cursor exposes installed skills through its own interface. They do share the same safety boundary: plugin installation changes no store code; implementation begins only after the merchant approves the plan.

## Natural request examples

- “Prepare this store for shopping agents.”
- “Make this ecommerce site agent-ready.”
- “Review catalog, cart, and checkout for agentic commerce.”

The merchant never needs to know an internal Skill name.
