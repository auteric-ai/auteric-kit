"""The one-command setup leaves usable instructions in each supported editor."""

import tempfile
import unittest
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "runtime" / "sdk" / "src"))
from auteric_edge.onboarding import prepare


class AssistantInstallTests(unittest.TestCase):
    def test_auto_installs_codex_copilot_claude_and_cursor_at_repository_root(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            backend = root / "services" / "api"
            backend.mkdir(parents=True)
            result = prepare(str(backend), "auto", instructions_root=str(root))
            self.assertEqual([item["client"] for item in result["skill"]], ["codex", "claude-code", "cursor"])
            self.assertTrue((root / ".agents/skills/auteric-commerce/SKILL.md").exists())
            self.assertTrue((root / ".claude/skills/auteric-commerce/SKILL.md").exists())
            self.assertTrue((root / ".cursor/rules/auteric-commerce.mdc").exists())
            self.assertTrue((backend / ".auteric/capabilities.json").exists())
            self.assertTrue(all(not item["changed"] for item in prepare(str(backend), "auto", instructions_root=str(root))["skill"]))

    def test_existing_custom_instruction_is_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            path = root / ".agents/skills/auteric-commerce/SKILL.md"
            path.parent.mkdir(parents=True)
            path.write_text("merchant custom instruction")
            result = prepare(str(root), "auto")
            self.assertTrue(result["skill"][0]["conflict"])
            self.assertEqual(path.read_text(), "merchant custom instruction")
            self.assertTrue((root / ".claude/skills/auteric-commerce/SKILL.md").exists())


if __name__ == "__main__":
    unittest.main()
