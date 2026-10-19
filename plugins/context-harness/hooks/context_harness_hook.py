#!/usr/bin/env python3
"""Hook command for Claude Code and Codex: `context_harness_hook.py pre-tool-use|stop`, hook JSON on stdin.

The logic lives in the bundled runtime (context_harness/hooks.py, standard library only). Without an
active context-harness task the hooks print nothing. They fail open: a missing or broken runtime never
blocks the agent.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills" / "context-harness" / "scripts"))
try:
    from context_harness.hooks import main
except Exception:  # noqa: BLE001 - fail open
    raise SystemExit(0)

raise SystemExit(main())
