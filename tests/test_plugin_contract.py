import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "plugins" / "auteric-kit"


class PluginContractTests(unittest.TestCase):
    def test_codex_manifest_has_user_facing_starters(self):
        manifest = json.loads((PLUGIN / ".codex-plugin" / "plugin.json").read_text())
        self.assertEqual(manifest["version"], "0.4.4")
        prompts = manifest["interface"]["defaultPrompt"]
        self.assertTrue(any("Prepare this storefront" in prompt for prompt in prompts))
        self.assertFalse(any("auteric-connect" in prompt for prompt in prompts))

    def test_natural_language_trigger_and_guided_approval_are_present(self):
        skill = (PLUGIN / "skills" / "auteric-connect" / "SKILL.md").read_text()
        self.assertIn("make a store agent-ready", skill)
        self.assertIn("Wait for normal user approval", skill)
        self.assertIn("Completed locally", skill)
        self.assertIn("Requires Auteric credentials/service", skill)

    def test_guided_implementation_reference_exists(self):
        guide = PLUGIN / "skills" / "auteric-connect" / "references" / "implementation.md"
        self.assertTrue(guide.is_file())
        self.assertIn("Do not write placeholder values", guide.read_text())

    def test_claude_has_a_matching_visible_entry_point(self):
        manifest = json.loads((PLUGIN / ".claude-plugin" / "plugin.json").read_text())
        command = (PLUGIN / "commands" / "prepare-storefront.md").read_text()
        self.assertEqual(manifest["version"], "0.4.4")
        self.assertIn("Prepare this storefront for shopping agents", command)
        self.assertNotIn("auteric-connect", command)


if __name__ == "__main__":
    unittest.main()
