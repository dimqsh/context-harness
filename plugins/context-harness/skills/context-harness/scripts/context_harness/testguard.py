"""Pre-existing tests are contracts. Snapshot them at task start, flag removed assertions.

The observed failure mode: an agent changes behaviour (e.g. mousedown -> pointerdown),
then edits the old test so it passes, and a hidden suite with the original test fails.
Additions to an old test file are fine; removed or rewritten lines need a justification.
`run_original` temporarily restores the original test files and runs them against the
current code, then always puts the agent's versions back.
"""
from __future__ import annotations

import difflib
import hashlib
import json
import os
import re
import shlex
import subprocess
from pathlib import Path

DEFAULT_PATTERNS = (
    r"(^|/)(tests?|__tests__|spec|specs)/",
    r"\.(test|spec)\.[cm]?[jt]sx?$",
    r"(^|/)test_[^/]*\.py$",
    r"_test\.(py|go)$",
    r"_spec\.rb$",
    r"(Test|Tests)\.(java|kt|cs)$",
)
SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "dist", "build", "__pycache__", ".next",
             "target", "coverage", ".tox", ".mypy_cache", ".pytest_cache"}
ASSERTION = re.compile(r"expect\s*\(|\bassert|fireEvent\.|userEvent\.|\.should\b|\.to[A-Z]\w*\(|"
                       r"\bt\.(Error|Fatal)|\brequire\.|toHaveBeenCalled|toEqual|toBe\b|self\.assert")
MAX_FILE_BYTES = 2 * 1024 * 1024
MAX_FILES = 20000
SHOWN_LINES = 12


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def list_files(root):
    try:
        tracked = subprocess.run(["git", "-C", str(root), "ls-files", "-z", "--cached", "--others",
                                  "--exclude-standard"], capture_output=True, timeout=60)
        if tracked.returncode == 0:
            names = [n.decode("utf-8", "surrogateescape") for n in tracked.stdout.split(b"\0") if n]
            return names[:MAX_FILES]
    except (OSError, subprocess.SubprocessError):
        pass
    names = []
    for folder, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for name in files:
            names.append(Path(folder, name).relative_to(root).as_posix())
            if len(names) >= MAX_FILES:
                return names
    return names


class TestGuard:
    def __init__(self, store, tid):
        self.store = store
        self.tid = tid
        self.folder = store.root / "test-snapshots" / tid
        self.blobs = self.folder / "blobs"
        self.manifest_path = self.folder / "manifest.json"
        self.active_path = self.folder / "guard-active.json"

    def exists(self):
        return self.manifest_path.is_file()

    def manifest(self):
        if not self.exists():
            raise ValueError("No test snapshot for this task; run tests-snapshot before editing")
        return json.loads(self.manifest_path.read_text(encoding="utf-8"))

    def snapshot(self, patterns=None, replace=False):
        if self.exists() and not replace:
            m = self.manifest()
            return {"task": self.tid, "snapshot_files": len(m["files"]), "created": False}
        patterns = list(patterns or DEFAULT_PATTERNS)
        compiled = [re.compile(p) for p in patterns]
        root = self.store.project
        files = {}
        self.blobs.mkdir(parents=True, exist_ok=True)
        for rel in list_files(root):
            if not any(c.search(rel) for c in compiled):
                continue
            path = root / rel
            try:
                if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_FILE_BYTES:
                    continue
                data = path.read_bytes()
            except OSError:
                continue
            digest = _sha(data)
            blob = self.blobs / digest
            if not blob.exists():
                blob.write_bytes(data)
            files[rel] = digest
        self.manifest_path.write_text(json.dumps({"patterns": patterns, "files": files}, indent=1),
                                      encoding="utf-8")
        with self.store.connect() as con:
            self.store.emit(con, "tests_snapshot", {"files": len(files)}, self.tid)
        return {"task": self.tid, "snapshot_files": len(files), "created": True}

    def _original(self, digest):
        return (self.blobs / digest).read_bytes()

    def check(self):
        self.restore_if_interrupted()
        m = self.manifest()
        root = self.store.project
        compiled = [re.compile(p) for p in m["patterns"]]
        modified, deleted, extended = [], [], []
        for rel, digest in sorted(m["files"].items()):
            path = root / rel
            if not path.is_file():
                deleted.append(rel)
                continue
            current = path.read_bytes()
            if _sha(current) == digest:
                continue
            old = self._original(digest).decode("utf-8", "replace").splitlines()
            new = current.decode("utf-8", "replace").splitlines()
            removed = [line for line in difflib.ndiff(old, new) if line.startswith("- ")]
            added = sum(1 for line in difflib.ndiff(old, new) if line.startswith("+ "))
            if not removed:
                extended.append({"path": rel, "added_lines": added})
                continue
            removed_text = [line[2:] for line in removed]
            assertions = [line.strip() for line in removed_text if ASSERTION.search(line)]
            modified.append({"path": rel, "removed_lines": len(removed_text), "added_lines": added,
                             "removed_assertions": assertions[:SHOWN_LINES],
                             "removed_sample": [line.strip() for line in removed_text[:SHOWN_LINES]]})
        new_files = [rel for rel in list_files(root)
                     if rel not in m["files"] and any(c.search(rel) for c in compiled)]
        result = {"task": self.tid, "snapshot_files": len(m["files"]), "ok": not modified and not deleted,
                  "modified": modified, "deleted": deleted, "extended_only": extended,
                  "new_test_files": new_files[:50]}
        if not result["ok"]:
            result["needs_justification"] = [x["path"] for x in modified] + deleted
            result["next"] = ("Pre-existing test lines were removed or rewritten. Prefer restoring them and "
                              "keeping the original behaviour; check with `guard --run`. Otherwise justify "
                              "each path in the report.")
        return result

    def flagged(self):
        data = self.check()
        return data, data.get("needs_justification", [])

    def restore_if_interrupted(self):
        """A crashed guard run must not leave original tests in place of the agent's versions."""
        if not self.active_path.exists():
            return False
        active = json.loads(self.active_path.read_text(encoding="utf-8"))
        root = self.store.project
        for rel, backup in active["backups"].items():
            path = root / rel
            if backup is None:
                if path.exists():
                    path.unlink()
            else:
                path.write_bytes((self.folder / "guard-backup" / backup).read_bytes())
        self.active_path.unlink()
        return True

    def run_original(self, command_template, runner, *, agent=None, cwd=None, timeout=900):
        """Swap in original versions of flagged files, run the command, always swap back."""
        data, flagged = self.flagged()
        if not flagged:
            return {"ran": False, "guard": data, "note": "No pre-existing test lines were removed; nothing to run."}
        if "{files}" not in command_template:
            raise ValueError("Command template must contain {files}, e.g. 'npx vitest run {files}'")
        m = self.manifest()
        root = self.store.project
        workdir = (root / (cwd or ".")).resolve()
        if not workdir.is_relative_to(root):
            raise ValueError("Guard directory must stay inside the project")
        backup_dir = self.folder / "guard-backup"
        backup_dir.mkdir(parents=True, exist_ok=True)
        backups = {}
        for rel in flagged:
            path = root / rel
            if path.exists():
                data_now = path.read_bytes()
                name = _sha(data_now)
                (backup_dir / name).write_bytes(data_now)
                backups[rel] = name
            else:
                backups[rel] = None
        self.active_path.write_text(json.dumps({"backups": backups}), encoding="utf-8")
        try:
            for rel in flagged:
                path = root / rel
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(self._original(m["files"][rel]))
            relative = [os.path.relpath(root / rel, workdir) for rel in flagged]
            command = command_template.replace("{files}", " ".join(shlex.quote(r) for r in relative))
            result = runner.run(command, kind="other", tid=self.tid, agent=agent, cwd=cwd, timeout=timeout)
        finally:
            self.restore_if_interrupted()
        with self.store.connect() as con:
            self.store.emit(con, "tests_guard_run", {"files": flagged, "exit_code": result.get("exit_code")},
                            self.tid)
        if result.get("refused"):
            return {"ran": False, "files": flagged, "verdict": "Guard run refused: " + " ".join(result["reasons"]),
                    "run": result, "guard": data}
        verdict = ("Original pre-existing tests pass against the current code."
                   if result.get("exit_code") == 0 else
                   "Original pre-existing tests FAIL against the current code: the change breaks an existing "
                   "contract. Restore compatible behaviour, or justify it explicitly in the report.")
        return {"ran": True, "files": flagged, "verdict": verdict, "run": result, "guard": data}
