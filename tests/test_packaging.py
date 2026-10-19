"""Release layout: one repo is a Claude Code marketplace, a Codex plugin, a skill and a Python package."""
import json
from pathlib import Path
import re
import subprocess
import sys
import unittest

import context_harness

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "plugins" / "context-harness"
SKILL = PLUGIN / "skills" / "context-harness"


def load(path):
    return json.loads(path.read_text(encoding="utf-8"))


class Packaging(unittest.TestCase):
    def test_versions_agree_everywhere(self):
        """pyproject uses PEP 440 (0.5.0a1); plugin manifests need semver (0.5.0-alpha.1) for `claude plugin tag`."""
        pyproject = re.search(r'^version = "([^"]+)"', (ROOT / "pyproject.toml").read_text(), re.M).group(1)
        self.assertEqual(context_harness.__version__, pyproject)
        market = load(ROOT / ".claude-plugin" / "marketplace.json")
        entry = [p for p in market["plugins"] if p["name"] == "context-harness"][0]
        manifests = {"marketplace": market["metadata"]["version"], "marketplace entry": entry["version"],
                     "claude plugin": load(PLUGIN / ".claude-plugin" / "plugin.json")["version"],
                     "codex plugin": load(PLUGIN / ".codex-plugin" / "plugin.json")["version"]}
        self.assertEqual(len(set(manifests.values())), 1, manifests)
        semver = manifests["claude plugin"]
        match = re.fullmatch(r"(\d+\.\d+\.\d+)(?:-(alpha|beta|rc)\.(\d+))?", semver)
        self.assertIsNotNone(match, f"{semver} is not the semver form claude plugin tag accepts")
        release, stage, number = match.groups()
        pep440 = release + ({"alpha": "a", "beta": "b", "rc": "rc"}[stage] + number if stage else "")
        self.assertEqual(pep440, pyproject)
        self.assertIn(f"## [{pyproject}]", (ROOT / "CHANGELOG.md").read_text())

    def test_manifests_point_at_existing_files(self):
        market = load(ROOT / ".claude-plugin" / "marketplace.json")
        source = ROOT / market["plugins"][0]["source"]
        self.assertEqual(source.resolve(), PLUGIN.resolve())
        codex = load(PLUGIN / ".codex-plugin" / "plugin.json")
        for key in ("skills", "mcpServers", "hooks"):
            self.assertTrue((PLUGIN / codex[key]).exists(), key)
        self.assertTrue((PLUGIN / codex["skills"] / "context-harness" / "SKILL.md").is_file())

    def test_mcp_configs_resolve_inside_the_plugin(self):
        claude = load(PLUGIN / ".mcp.json")["mcpServers"]["context-harness"]
        script = claude["args"][0].replace("${CLAUDE_PLUGIN_ROOT}", str(PLUGIN))
        self.assertTrue(Path(script).is_file(), script)
        # Codex does not expand ${CLAUDE_PLUGIN_ROOT} in MCP args; it resolves `cwd` against the plugin root.
        codex = load(PLUGIN / ".codex-mcp.json")["mcpServers"]["context-harness"]
        self.assertEqual(codex["cwd"], ".")
        self.assertTrue((PLUGIN / codex["args"][0]).is_file())
        self.assertNotIn("${", json.dumps(codex))

    def test_hook_commands_resolve_inside_the_plugin(self):
        config = load(PLUGIN / "hooks" / "hooks.json")["hooks"]
        self.assertEqual(sorted(config), ["PreToolUse", "Stop"])
        self.assertEqual(config["PreToolUse"][0]["matcher"], "Bash")
        for event, groups in config.items():
            for hook in groups[0]["hooks"]:
                path = re.search(r'"\$\{CLAUDE_PLUGIN_ROOT\}/([^"]+)"', hook["command"]).group(1)
                self.assertTrue((PLUGIN / path).is_file(), (event, path))

    def test_committed_plugin_skill_matches_src(self):
        out = subprocess.run([sys.executable, str(ROOT / "scripts" / "bundle_skill.py"), "--check"],
                             capture_output=True, text=True, timeout=60)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)

    def test_license_travels_with_plugin_and_skill_installs(self):
        text = (ROOT / "LICENSE").read_text(encoding="utf-8")
        self.assertIn("MIT License", text)
        for copy in (PLUGIN / "LICENSE", ROOT / "src" / "context_harness" / "skill_data" / "LICENSE",
                     SKILL / "LICENSE"):
            self.assertEqual(copy.read_text(encoding="utf-8"), text, copy)
        for manifest in (PLUGIN / ".claude-plugin" / "plugin.json", PLUGIN / ".codex-plugin" / "plugin.json"):
            self.assertEqual(load(manifest)["license"], "MIT")
        self.assertIn('license = "MIT"', (ROOT / "pyproject.toml").read_text())

    def test_skill_frontmatter_names_the_skill(self):
        head = (SKILL / "SKILL.md").read_text(encoding="utf-8").split("---")[1]
        self.assertIn("name: context-harness", head)
        self.assertIn("description:", head)

    def test_no_absolute_home_paths_are_shipped(self):
        pattern = re.compile("/" + "(Users|home)/[A-Za-z0-9._-]+/")
        skip = {".git", "__pycache__", "build", "dist", ".venv"}
        offenders = []
        for path in ROOT.rglob("*"):
            if not path.is_file() or skip & set(path.relative_to(ROOT).parts) or path.suffix in (".pyc",):
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            if pattern.search(text):
                offenders.append(str(path.relative_to(ROOT)))
        self.assertEqual(offenders, [])


if __name__ == "__main__":
    unittest.main()
