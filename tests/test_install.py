"""v0.5 acceptance: skill + MCP install for several agents; dry run by default, backups on write."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from context_harness import install as inst


class Install(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.home = self.root / "home"
        self.home.mkdir()
        self.skill = self.root / "bundle" / "context-harness"
        (self.skill / "scripts" / "context_harness").mkdir(parents=True)
        (self.skill / "SKILL.md").write_text("---\nname: context-harness\n---\n", encoding="utf-8")
        (self.skill / "scripts" / "ch.py").write_text("print('ch')\n", encoding="utf-8")
        (self.skill / "scripts" / "context_harness" / "mcp_server.py").write_text("", encoding="utf-8")
        patches = [mock.patch.dict(os.environ, {"HOME": str(self.home), "PATH": "/usr/bin:/bin",
                                                "CONTEXT_HARNESS_HOME": str(self.home / ".context-harness")}),
                   mock.patch.object(inst, "skill_source", return_value=self.skill)]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        os.environ.pop("CODEX_HOME", None)

    def run_install(self, **kw):
        kw.setdefault("python", "/usr/bin/python3")
        return inst.install(**kw)

    def test_dry_run_changes_nothing(self):
        out = self.run_install(skills=["claude-code", "codex"], mcps=["cursor", "codex", "claude-desktop", "gemini"])
        self.assertFalse(out["write"])
        self.assertEqual(list(self.home.iterdir()), [])
        self.assertEqual(out["runtime"], str(self.home / ".claude/skills/context-harness/scripts/ch.py"))
        self.assertIn("Dry run", out["note"])

    def test_write_installs_skills_and_registers_servers(self):
        out = self.run_install(skills=["claude-code", "codex"],
                               mcps=["cursor", "codex", "claude-desktop", "gemini"], write=True)
        runtime = str(self.home / ".claude/skills/context-harness/scripts/ch.py")
        for target in (".claude/skills/context-harness", ".codex/skills/context-harness"):
            self.assertTrue((self.home / target / "SKILL.md").is_file())
        cursor = json.loads((self.home / ".cursor/mcp.json").read_text())
        self.assertEqual(cursor["mcpServers"]["context-harness"]["args"], [runtime, "mcp"])
        self.assertEqual(cursor["mcpServers"]["context-harness"]["env"], {"PATH": "/usr/bin:/bin"})
        gemini = json.loads((self.home / ".gemini/settings.json").read_text())
        self.assertNotIn("env", gemini["mcpServers"]["context-harness"])
        desktop = [s for s in out["steps"] if s["target"] == "claude-desktop" and s["kind"] == "mcp"][0]
        self.assertTrue(Path(desktop["path"]).is_file())
        toml = (self.home / ".codex/config.toml").read_text()
        self.assertIn("[mcp_servers.context-harness]", toml)
        self.assertIn(f'args = ["{runtime}", "mcp"]', toml)

    def test_existing_config_is_preserved_backed_up_and_not_overwritten_without_force(self):
        cursor = self.home / ".cursor" / "mcp.json"
        cursor.parent.mkdir()
        cursor.write_text(json.dumps({"mcpServers": {"other": {"command": "x"},
                                                     "context-harness": {"command": "old"}}}))
        out = self.run_install(mcps=["cursor"], write=True)
        self.assertIn("exists", out["steps"][0]["action"])
        self.assertEqual(json.loads(cursor.read_text())["mcpServers"]["context-harness"], {"command": "old"})
        out = self.run_install(mcps=["cursor"], write=True, force=True)
        data = json.loads(cursor.read_text())
        self.assertEqual(data["mcpServers"]["other"], {"command": "x"})
        self.assertEqual(data["mcpServers"]["context-harness"]["command"], "/usr/bin/python3")
        self.assertTrue(Path(out["steps"][0]["backup"]).is_file())
        again = self.run_install(mcps=["cursor"], write=True)
        self.assertEqual(again["steps"][0]["action"], "unchanged")

    def test_invalid_json_is_never_rewritten(self):
        cursor = self.home / ".cursor" / "mcp.json"
        cursor.parent.mkdir()
        cursor.write_text("{broken")
        out = self.run_install(mcps=["cursor"], write=True, force=True)
        self.assertIn("not valid JSON", out["steps"][0]["action"])
        self.assertEqual(cursor.read_text(), "{broken")

    def test_codex_toml_section_is_replaced_only_with_force(self):
        config = self.home / ".codex" / "config.toml"
        config.parent.mkdir()
        config.write_text('model = "gpt"\n\n[mcp_servers.context-harness]\ncommand = "old"\nargs = []\n'
                          '[mcp_servers.context-harness.env]\nA = "1"\n\n[mcp_servers.other]\ncommand = "o"\n')
        out = self.run_install(mcps=["codex"], write=True)
        self.assertIn("exists", out["steps"][0]["action"])
        self.run_install(mcps=["codex"], write=True, force=True)
        text = config.read_text()
        self.assertIn('model = "gpt"', text)
        self.assertIn("[mcp_servers.other]", text)
        self.assertNotIn('command = "old"', text)
        self.assertNotIn('A = "1"', text)
        self.assertEqual(text.count("[mcp_servers.context-harness]"), 1)
        self.assertEqual(self.run_install(mcps=["codex"], write=True)["steps"][0]["action"], "unchanged")

    def test_skill_is_not_overwritten_without_force(self):
        self.run_install(skills=["codex"], write=True)
        marker = self.home / ".codex/skills/context-harness/LOCAL"
        marker.write_text("mine")
        self.assertIn("exists", self.run_install(skills=["codex"], write=True)["steps"][0]["action"])
        self.assertTrue(marker.exists())
        out = self.run_install(skills=["codex"], write=True, force=True)
        self.assertFalse(marker.exists())
        backup = Path(out["steps"][0]["backup"])
        self.assertTrue((backup / "LOCAL").exists())
        self.assertFalse(backup.is_relative_to(self.home / ".codex" / "skills"),
                         "a backup inside skills/ would be loaded as a second skill")
        self.assertEqual(sorted(p.name for p in (self.home / ".codex" / "skills").iterdir()), ["context-harness"])

    def test_project_scoped_targets(self):
        project = self.root / "project"
        project.mkdir()
        self.run_install(skills=["claude-project"], mcps=["claude-project"], project=project, write=True)
        self.assertTrue((project / ".claude/skills/context-harness/SKILL.md").is_file())
        data = json.loads((project / ".mcp.json").read_text())
        self.assertIn("context-harness", data["mcpServers"])

    def test_claude_code_uses_its_cli(self):
        out = self.run_install(mcps=["claude-code", "json"])
        step = out["steps"][0]
        self.assertEqual(step["command"][:6], ["claude", "mcp", "add", "--scope", "user", "context-harness"])
        self.assertEqual(out["steps"][1]["config"]["mcpServers"]["context-harness"]["command"], "/usr/bin/python3")
        with mock.patch.object(inst.shutil, "which", return_value=None):
            skipped = self.run_install(mcps=["claude-code"], write=True)
        self.assertIn("not on PATH", skipped["steps"][0]["action"])

    def test_unknown_targets_fail_loudly(self):
        with self.assertRaises(ValueError):
            self.run_install(mcps=["vim"])
        with self.assertRaises(ValueError):
            self.run_install(skills=["relative/dir"])
        with self.assertRaises(ValueError):
            inst.install()


class Bundle(unittest.TestCase):
    def make_package(self, root):
        package = root / "src" / "context_harness"
        (package / "skill_data" / "references").mkdir(parents=True)
        (package / "cli.py").write_text("def main():\n    return 0\n")
        (package / "skill_data" / "SKILL.md").write_text("---\nname: context-harness\n---\n")
        (package / "skill_data" / "references" / "install.md").write_text("# Install\n")
        return package

    def test_bundle_makes_a_self_contained_skill(self):
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp)
            self.make_package(repo)
            stale = repo / "plugins" / "context-harness" / "skills" / "context-harness" / "OLD"
            stale.parent.mkdir(parents=True)
            stale.write_text("removed by a rebundle")
            skill = inst.bundle(repo)
            self.assertEqual(skill, repo / "plugins" / "context-harness" / "skills" / "context-harness")
            self.assertTrue((skill / "SKILL.md").is_file())
            self.assertTrue((skill / "references" / "install.md").is_file())
            self.assertTrue((skill / "scripts" / "context_harness" / "cli.py").is_file())
            self.assertFalse((skill / "scripts" / "context_harness" / "skill_data").exists(),
                             "a nested SKILL.md would be loaded as a second skill")
            self.assertIn("sys.path.insert", (skill / "scripts" / "ch.py").read_text())
            self.assertFalse(stale.exists())

    def test_installed_package_assembles_the_skill_and_serves_mcp_as_a_module(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            site = root / "site-packages" / "context_harness"
            site.parent.mkdir()
            self.make_package(root).rename(site)
            home = root / "home"
            home.mkdir()
            env = {"HOME": str(home), "CONTEXT_HARNESS_HOME": str(home / ".context-harness")}
            with mock.patch.dict(os.environ, env), mock.patch.object(inst, "package_dir", return_value=site):
                os.environ.pop("CODEX_HOME", None)
                self.assertEqual(inst.skill_source(), site)
                out = inst.install(mcps=["json"], python="/venv/bin/python")
                spec = out["steps"][0]["config"]["mcpServers"]["context-harness"]
                self.assertEqual(spec, {"command": "/venv/bin/python", "args": ["-m", "context_harness", "mcp"]})
                inst.install(skills=["codex"], write=True)
            skill = home / ".codex" / "skills" / "context-harness"
            self.assertTrue((skill / "SKILL.md").is_file())
            self.assertTrue((skill / "scripts" / "context_harness" / "cli.py").is_file())
            self.assertTrue((skill / "scripts" / "ch.py").is_file())

    def test_source_checkout_serves_mcp_from_scripts_ch_py(self):
        repo = Path(__file__).resolve().parents[1]
        with mock.patch.object(inst, "package_dir", return_value=repo / "src" / "context_harness"):
            self.assertEqual(inst.mcp_runtime([]), [str(repo / "scripts" / "ch.py")])


if __name__ == "__main__":
    unittest.main()
