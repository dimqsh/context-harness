"""Install the skill and/or register the MCP server for several agents.

Dry run by default: it prints what would change. `--write` applies it, keeps a timestamped
backup of every config file it touches and never overwrites an existing install without
`--force`. Nothing here talks to the network.
"""
from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
import sys
import time
from pathlib import Path

NAME = "context-harness"
SKILL_TARGETS = ("claude-code", "codex", "claude-project")
MCP_TARGETS = ("claude-code", "claude-project", "codex", "cursor", "claude-desktop", "gemini", "json")
GUI_CLIENTS = ("cursor", "claude-desktop")  # started without a shell profile: pin PATH for `run`


def _stamp():
    return time.strftime("%Y%m%d-%H%M%S")


def package_dir():
    return Path(__file__).resolve().parent


PLUGIN_SKILL = Path("plugins") / NAME / "skills" / NAME
LAUNCHER = ("#!/usr/bin/env python3\n"
            "import sys\nfrom pathlib import Path\n\n"
            "sys.path.insert(0, str(Path(__file__).resolve().parent))\n"
            "from context_harness.cli import main\n\nraise SystemExit(main())\n")


def assemble_skill(dest, package=None):
    """Write a self-contained skill folder: SKILL.md etc. from `skill_data`, the package as its runtime."""
    package = Path(package or package_dir())
    data = package / "skill_data"
    if not (data / "SKILL.md").is_file():
        raise ValueError(f"Skill data not found in {package}")
    dest = Path(dest)
    shutil.copytree(data, dest, dirs_exist_ok=True,
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".DS_Store"))
    shutil.copytree(package, dest / "scripts" / "context_harness", dirs_exist_ok=True,
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".DS_Store", "skill_data"))
    launcher = dest / "scripts" / "ch.py"
    launcher.write_text(LAUNCHER, encoding="utf-8")
    launcher.chmod(0o755)
    return dest


def bundle(repo_root):
    """Regenerate the plugin's skill folder from src; it is committed so plugin installs work from git."""
    repo_root = Path(repo_root)
    dest = repo_root / PLUGIN_SKILL
    if dest.exists():
        shutil.rmtree(dest)
    return assemble_skill(dest, repo_root / "src" / "context_harness")


def skill_source():
    """The bundled skill folder we run from (plugin or skill install), else this package (pip or checkout)."""
    here = package_dir()
    bundled = here.parents[1]
    if (bundled / "SKILL.md").is_file():
        return bundled
    if (here / "skill_data" / "SKILL.md").is_file():
        return here
    raise ValueError("Skill files not found; reinstall the package or the plugin")


def copy_skill(src, dest):
    if (src / "SKILL.md").is_file():
        shutil.copytree(src, dest, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    else:
        assemble_skill(dest, src)


def mcp_runtime(installed):
    """Arguments before `mcp`: an installed skill's ch.py, the bundled one, a checkout's, or `-m context_harness`."""
    if installed:
        return [str(installed[0] / "scripts" / "ch.py")]
    src = skill_source()
    if (src / "SKILL.md").is_file():
        return [str(src / "scripts" / "ch.py")]
    checkout = src.parents[1] / "scripts" / "ch.py"
    if src.parent.name == "src" and checkout.is_file():
        return [str(checkout)]
    return ["-m", "context_harness"]


def skill_dest(target, project=None):
    home = Path.home()
    if target == "claude-code":
        return home / ".claude" / "skills" / NAME
    if target == "codex":
        return Path(os.environ.get("CODEX_HOME", home / ".codex")).expanduser() / "skills" / NAME
    if target == "claude-project":
        if not project:
            raise ValueError("claude-project needs --target-project")
        return Path(project).expanduser().resolve() / ".claude" / "skills" / NAME
    path = Path(target).expanduser()
    if not path.is_absolute():
        raise ValueError(f"Unknown skill target {target!r}; use {', '.join(SKILL_TARGETS)} or an absolute folder")
    return path / NAME if path.name != NAME else path


def install_skill(target, project, write, force):
    src = skill_source()
    dest = skill_dest(target, project)
    step = {"kind": "skill", "target": target, "path": str(dest), "source": str(src)}
    if dest.exists():
        if not force:
            return {**step, "action": "exists (use --force to replace; a backup is kept)"}
        step["action"] = "replace"
        if write:
            # Never next to the skill: agents scan skills/*/SKILL.md and would load the backup too.
            from .home import harness_home
            backup = harness_home() / "backups" / "skills" / f"{target if target in SKILL_TARGETS else 'dir'}-{NAME}-{_stamp()}"
            backup.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(dest), str(backup))
            step["backup"] = str(backup)
    else:
        step["action"] = "create"
    if write:
        dest.parent.mkdir(parents=True, exist_ok=True)
        copy_skill(src, dest)
        step["done"] = True
    return step


def server_spec(runtime, python, gui=False):
    spec = {"command": python, "args": [*runtime, "mcp"]}
    if gui and os.environ.get("PATH"):
        spec["env"] = {"PATH": os.environ["PATH"]}
    return spec


def _claude_desktop_config():
    system = platform.system()
    if system == "Darwin":
        return Path.home() / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json"
    if system == "Windows":
        return Path(os.environ.get("APPDATA", Path.home())) / "Claude" / "claude_desktop_config.json"
    return Path.home() / ".config" / "Claude" / "claude_desktop_config.json"


def _json_merge(path, key, spec, write, force):
    step = {"path": str(path)}
    data = {}
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8") or "{}")
        except json.JSONDecodeError as exc:
            return {**step, "action": f"skipped: existing file is not valid JSON ({exc})"}
        if not isinstance(data, dict):
            return {**step, "action": "skipped: existing file is not a JSON object"}
    servers = data.setdefault(key, {})
    if servers.get(NAME) == spec:
        return {**step, "action": "unchanged"}
    if NAME in servers and not force:
        return {**step, "action": "exists with different settings (use --force)", "existing": servers[NAME]}
    servers[NAME] = spec
    step["action"] = "update" if path.exists() else "create"
    step["config"] = {key: {NAME: spec}}
    if write:
        if path.exists():
            backup = path.with_name(path.name + ".bak-context-harness-" + _stamp())
            shutil.copy2(path, backup)
            step["backup"] = str(backup)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        step["done"] = True
    return step


def _toml_section(spec):
    lines = [f"[mcp_servers.{NAME}]", "command = " + json.dumps(spec["command"]),
             "args = [" + ", ".join(json.dumps(a) for a in spec["args"]) + "]"]
    if spec.get("env"):
        lines.append(f"[mcp_servers.{NAME}.env]")
        lines += [f"{k} = {json.dumps(v)}" for k, v in spec["env"].items()]
    return "\n".join(lines) + "\n"


def _codex_merge(spec, write, force):
    path = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")).expanduser() / "config.toml"
    section = _toml_section(spec)
    step = {"path": str(path), "config": section}
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    header = f"[mcp_servers.{NAME}]"
    lines = text.splitlines(keepends=True)
    present = any(line.strip() == header for line in lines)
    if present:
        kept, removed, skipping = [], [], False
        for line in lines:
            stripped = line.strip()
            if stripped.startswith("["):
                skipping = stripped == header or stripped.startswith(f"[mcp_servers.{NAME}.")
            (removed if skipping else kept).append(line)
        old_section = "".join(removed)
        if old_section.strip() == section.strip():
            return {**step, "action": "unchanged"}
        if not force:
            return {**step, "action": "exists with different settings (use --force)"}
        text = "".join(kept)
        step["action"] = "update"
    else:
        step["action"] = "update" if path.exists() else "create"
    new_text = text + ("\n" if text and not text.endswith("\n") else "") + ("\n" if text.strip() else "") + section
    if write:
        if path.exists():
            backup = path.with_name(path.name + ".bak-context-harness-" + _stamp())
            shutil.copy2(path, backup)
            step["backup"] = str(backup)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(new_text, encoding="utf-8")
        step["done"] = True
    return step


def register_mcp(target, runtime, python, project, write, force):
    spec = server_spec(runtime, python, gui=target in GUI_CLIENTS)
    step = {"kind": "mcp", "target": target}
    if target == "claude-code":
        argv = ["claude", "mcp", "add", "--scope", "user", NAME, "--", spec["command"], *spec["args"]]
        step["command"] = argv
        if not write:
            return {**step, "action": "would run"}
        if not shutil.which("claude"):
            return {**step, "action": "skipped: `claude` CLI not on PATH; run the command yourself"}
        out = subprocess.run(argv, capture_output=True, text=True, timeout=60)
        return {**step, "action": "ran", "exit_code": out.returncode,
                "output": (out.stdout + out.stderr).strip()[-500:], "done": out.returncode == 0}
    if target == "claude-project":
        if not project:
            raise ValueError("claude-project needs --target-project")
        path = Path(project).expanduser().resolve() / ".mcp.json"
        return {**step, **_json_merge(path, "mcpServers", spec, write, force)}
    if target == "codex":
        return {**step, **_codex_merge(spec, write, force)}
    if target == "cursor":
        return {**step, **_json_merge(Path.home() / ".cursor" / "mcp.json", "mcpServers", spec, write, force)}
    if target == "claude-desktop":
        return {**step, **_json_merge(_claude_desktop_config(), "mcpServers", spec, write, force)}
    if target == "gemini":
        return {**step, **_json_merge(Path.home() / ".gemini" / "settings.json", "mcpServers", spec, write, force)}
    if target == "json":
        return {**step, "action": "print only", "config": {"mcpServers": {NAME: spec}}}
    raise ValueError(f"Unknown MCP target {target!r}; use {', '.join(MCP_TARGETS)}")


def install(skills=(), mcps=(), python=None, project=None, write=False, force=False):
    if not skills and not mcps:
        raise ValueError("Choose at least one --skill or --mcp target")
    steps = [install_skill(t, project, write, force) for t in skills]
    installed = [Path(s["path"]) for s in steps
                 if s.get("done") or (not write and s["action"] in ("create", "replace"))
                 or (s["action"].startswith("exists")
                     and (Path(s["path"]) / "scripts" / "context_harness" / "mcp_server.py").is_file())]
    runtime = mcp_runtime(installed)
    python = python or sys.executable
    steps += [register_mcp(t, runtime, python, project, write, force) for t in mcps]
    return {"write": write, "runtime": " ".join(runtime), "python": python, "steps": steps,
            "note": None if write else "Dry run: nothing changed. Re-run with --write to apply."}
