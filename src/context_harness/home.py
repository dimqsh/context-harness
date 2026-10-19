"""Where state lives when no explicit --state is given.

One state directory per project, outside the project tree, so the same harness works from
any agent (Codex, Claude Code, an MCP client) without remembering a state path.
"""
from __future__ import annotations

import hashlib
import os
import re
import subprocess
from pathlib import Path


def harness_home():
    return Path(os.environ.get("CONTEXT_HARNESS_HOME", "~/.context-harness")).expanduser()


def project_root(start=None):
    start = Path(start or os.getcwd()).expanduser().resolve()
    try:
        out = subprocess.run(["git", "-C", str(start), "rev-parse", "--show-toplevel"],
                             capture_output=True, text=True, timeout=15)
        if out.returncode == 0 and out.stdout.strip():
            return Path(out.stdout.strip()).resolve()
    except (OSError, subprocess.SubprocessError):
        pass
    return start


def default_state(project):
    project = Path(project).expanduser().resolve()
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", project.name).strip("-")[:40] or "project"
    digest = hashlib.sha256(str(project).encode("utf-8")).hexdigest()[:10]
    return harness_home() / "projects" / f"{slug}-{digest}"


def resolve_state(state=None, project=None):
    """Explicit state > CONTEXT_HARNESS_STATE > state derived from the project (or cwd's git root)."""
    if state:
        return Path(state).expanduser().resolve(), (Path(project).expanduser().resolve() if project else None)
    env = os.environ.get("CONTEXT_HARNESS_STATE")
    if env and not project:
        return Path(env).expanduser().resolve(), None
    root = Path(project).expanduser().resolve() if project else project_root()
    return default_state(root), root
