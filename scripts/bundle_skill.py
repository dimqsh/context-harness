#!/usr/bin/env python3
"""Regenerate plugins/context-harness/skills/context-harness from src/context_harness.

The plugin's skill folder is generated (SKILL.md and references from `skill_data`, the package as its
runtime) but committed, because plugin marketplaces install straight from git. `--check` exits 1 when
the committed copy is out of date.
"""
import argparse
import filecmp
import sys
import tempfile
from pathlib import Path

root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root / "src"))
from context_harness.install import PLUGIN_SKILL, assemble_skill, bundle  # noqa: E402


def differences(left, right):
    """Relative paths that differ between two folders (missing on either side or different bytes)."""
    found = []

    def walk(cmp, prefix=""):
        found.extend(prefix + name for name in cmp.left_only + cmp.right_only + cmp.funny_files)
        found.extend(prefix + name for name in cmp.common_files
                     if not filecmp.cmp(Path(cmp.left, name), Path(cmp.right, name), shallow=False))
        for name, sub in cmp.subdirs.items():
            walk(sub, prefix + name + "/")

    walk(filecmp.dircmp(left, right, ignore=["__pycache__", ".DS_Store"]))
    return sorted(found)


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="Only compare; exit 1 if the bundle is stale")
    args = parser.parse_args()
    if not args.check:
        print(bundle(root))
        return 0
    with tempfile.TemporaryDirectory() as temp:
        fresh = assemble_skill(Path(temp) / "skill", root / "src" / "context_harness")
        stale = differences(fresh, root / PLUGIN_SKILL)
    if stale:
        print("Plugin skill bundle is out of date; run scripts/bundle_skill.py:\n  " + "\n  ".join(stale))
        return 1
    print("Plugin skill bundle is up to date.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
