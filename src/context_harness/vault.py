"""Explicit, project-bound output snapshots. Contents are untrusted evidence, never instructions."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import stat
import tempfile


MAX_OUTPUT_BYTES = 16 * 1024 * 1024
CHUNK_LINES = 40


def _size(value):
    # The CLI uses this representation. Include its terminating newline.
    return len((json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))


def _budget(value):
    if isinstance(value, bool) or not isinstance(value, int) or value < 512:
        raise ValueError("budget_bytes must be an integer of at least 512")
    return value


def _regular_bytes(path):
    """Bound every read, including snapshots whose paths were replaced after ingest."""
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    fd = os.open(path, flags)
    with os.fdopen(fd, "rb") as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise ValueError("Output must be a regular file")
        if info.st_size > MAX_OUTPUT_BYTES:
            raise ValueError("Output exceeds 16 MiB limit")
        data = handle.read(MAX_OUTPUT_BYTES + 1)
    if len(data) > MAX_OUTPUT_BYTES:
        raise ValueError("Output exceeds 16 MiB limit")
    return data


def _lines(text):
    return text.splitlines(keepends=True)


class OutputVault:
    """A source snapshot is immutable; its original path is provenance, not live context."""

    def __init__(self, store):
        self.store = store
        self.blobs = store.root / "output-vault" / "blobs"

    def _schema(self, con):
        # Lazy and additive, using the project's existing state database.
        con.execute("""CREATE TABLE IF NOT EXISTS vault_outputs(
            id TEXT PRIMARY KEY, sha256 TEXT NOT NULL UNIQUE, source TEXT NOT NULL,
            label TEXT NOT NULL, byte_count INTEGER NOT NULL, line_count INTEGER NOT NULL,
            created TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)""")
        con.execute("""CREATE VIRTUAL TABLE IF NOT EXISTS vault_chunks USING fts5(
            output_id UNINDEXED, start_line UNINDEXED, end_line UNINDEXED,
            label, content, tokenize='unicode61')""")

    def _input(self, path):
        path = Path(path).expanduser()
        if not path.is_absolute():
            path = self.store.project / path
        # Inspect the supplied path before resolve: do not silently follow symlinks.
        for part in (path, *path.parents):
            if part.is_symlink():
                raise ValueError("Output input must not contain symlinks")
        path = path.resolve(strict=True)
        name = path.name.lower()
        if (name.startswith(".env") or name.startswith("credentials")
                or name in {"id_rsa", "id_dsa", "id_ecdsa", "id_ed25519", "token.json"}
                or name.startswith("secrets.") or name in {"secrets", "credentials"}
                or path.suffix.lower() in {".pem", ".key", ".p12", ".pfx"}):
            raise ValueError("Refusing a credential-like output filename")
        data = _regular_bytes(path)
        try:
            text = data.decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise ValueError("Output must be valid UTF-8") from exc
        if "\x00" in text:
            raise ValueError("Output must be text without NUL bytes")
        return path, data, text

    def ingest(self, path, label=""):
        if not isinstance(label, str) or len(label.encode("utf-8")) > 240:
            raise ValueError("label must be text of at most 240 UTF-8 bytes")
        path, data, text = self._input(path)
        digest = hashlib.sha256(data).hexdigest()
        output_id = "o_" + digest
        lines = _lines(text)
        with self.store.connect() as con:
            self._schema(con)
            existing = con.execute("SELECT * FROM vault_outputs WHERE id=?", (output_id,)).fetchone()
            if existing:
                self._content(dict(existing))  # Never hide a corrupt deduplicated snapshot.
            else:
                self.blobs.mkdir(parents=True, exist_ok=True)
                blob = self.blobs / digest
                if blob.exists():
                    if blob.is_symlink() or hashlib.sha256(_regular_bytes(blob)).hexdigest() != digest:
                        raise ValueError("Existing output blob is corrupt")
                else:
                    fd, temporary = tempfile.mkstemp(prefix=".ingest-", dir=self.blobs)
                    try:
                        with os.fdopen(fd, "wb") as handle:
                            handle.write(data)
                        # Atomic publication without replacing existing immutable data.
                        try:
                            os.link(temporary, blob)
                        except FileExistsError:
                            if blob.is_symlink() or hashlib.sha256(_regular_bytes(blob)).hexdigest() != digest:
                                raise ValueError("Concurrent output blob is corrupt")
                    finally:
                        Path(temporary).unlink(missing_ok=True)
                con.execute("""INSERT INTO vault_outputs
                    (id,sha256,source,label,byte_count,line_count) VALUES (?,?,?,?,?,?)""",
                            (output_id, digest, str(path), label, len(data), len(lines)))
                for start in range(0, len(lines), CHUNK_LINES):
                    con.execute("INSERT INTO vault_chunks VALUES (?,?,?,?,?)",
                                (output_id, start + 1, min(start + CHUNK_LINES, len(lines)),
                                 label, "".join(lines[start:start + CHUNK_LINES])))
        return {"output_id": output_id, "sha256": digest, "bytes": len(data),
                "lines": len(lines), "chunks": (len(lines) + CHUNK_LINES - 1) // CHUNK_LINES,
                "deduplicated": bool(existing), "untrusted": True}

    def _content(self, row):
        digest = row["sha256"]
        if not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ValueError("Output metadata is corrupt")
        blob = self.blobs / digest
        try:
            if blob.is_symlink():
                raise ValueError("Output blob must not be a symlink")
            data = _regular_bytes(blob)
        except FileNotFoundError as exc:
            raise ValueError("Output blob is missing") from exc
        if len(data) != row["byte_count"] or hashlib.sha256(data).hexdigest() != digest:
            raise ValueError("Output blob SHA-256 mismatch")
        try:
            lines = _lines(data.decode("utf-8", errors="strict"))
        except UnicodeDecodeError as exc:
            raise ValueError("Output blob is corrupt: invalid UTF-8") from exc
        if len(lines) != row["line_count"]:
            raise ValueError("Output metadata line count mismatch")
        source = Path(row["source"])
        try:
            if source.is_symlink():
                source_status = "changed"
            else:
                source_status = "unchanged" if hashlib.sha256(_regular_bytes(source)).hexdigest() == digest else "changed"
        except (OSError, ValueError):
            source_status = "missing_or_unreadable"
        return lines, source_status

    def _row(self, output_id):
        with self.store.connect() as con:
            self._schema(con)
            row = con.execute("SELECT * FROM vault_outputs WHERE id=?", (output_id,)).fetchone()
        if row is None:
            raise ValueError("Unknown output_id")
        return dict(row)

    def read(self, output_id, start_line=1, max_lines=40, budget_bytes=6000):
        budget = _budget(budget_bytes)
        if (isinstance(start_line, bool) or not isinstance(start_line, int) or start_line < 1
                or isinstance(max_lines, bool) or not isinstance(max_lines, int) or not 1 <= max_lines <= 1000):
            raise ValueError("start_line must be positive; max_lines must be between 1 and 1000")
        row = self._row(output_id)
        lines, status = self._content(row)
        if start_line > max(len(lines), 1):
            raise ValueError("start_line is beyond the stored output")
        result = {"output_id": output_id, "sha256": row["sha256"], "untrusted": True,
                  "source_status": status, "total_lines": len(lines), "start_line": start_line,
                  "end_line": start_line - 1, "text": "", "truncated": start_line <= len(lines),
                  "next_line": start_line if lines else None}
        if _size(result) > budget:
            raise ValueError("budget_bytes is too small for output metadata")
        for index in range(start_line - 1, min(len(lines), start_line - 1 + max_lines)):
            candidate = dict(result, text=result["text"] + lines[index], end_line=index + 1,
                             truncated=index + 1 < len(lines), next_line=index + 2 if index + 1 < len(lines) else None)
            if _size(candidate) > budget:
                break
            result = candidate
        if lines and result["end_line"] < start_line:
            raise ValueError("The requested original line exceeds budget_bytes; increase the budget")
        return result

    def search(self, query, limit=5, budget_bytes=4000):
        budget = _budget(budget_bytes)
        if not isinstance(query, str) or len(query.encode("utf-8")) > 8000:
            raise ValueError("query must be text of at most 8000 UTF-8 bytes")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 20:
            raise ValueError("limit must be between 1 and 20")
        terms = list(dict.fromkeys(re.findall(r"\w+", query, flags=re.UNICODE)))[:32]
        result = {"untrusted": True, "results": [], "truncated": False,
                  "matched_chunks": 0, "omitted_matches": 0}
        if not terms:
            return result
        match = " OR ".join('"' + term.replace('"', '""') + '"' for term in terms)
        with self.store.connect() as con:
            self._schema(con)
            count = con.execute("SELECT count(*) FROM vault_chunks WHERE vault_chunks MATCH ?", (match,)).fetchone()[0]
            rows = [dict(row) for row in con.execute("""SELECT o.*,c.start_line,c.end_line,
                bm25(vault_chunks) AS rank FROM vault_chunks c JOIN vault_outputs o ON o.id=c.output_id
                WHERE vault_chunks MATCH ? ORDER BY rank,o.id,c.start_line LIMIT ?""", (match, limit))]
        result["matched_chunks"] = count
        result["omitted_matches"] = count
        result["truncated"] = bool(count)
        if _size(result) > budget:
            raise ValueError("budget_bytes is too small for search metadata")
        for row in rows:
            lines, status = self._content(row)
            start, end = int(row["start_line"]) - 1, int(row["end_line"])
            # Preserve exact source text and identifiers. Select a literal matching
            # line rather than replacing words or manufacturing a summary.
            anchor = next((i for i in range(start, end)
                           if any(term.casefold() in lines[i].casefold() for term in terms)), start)
            hit = {"output_id": row["id"], "sha256": row["sha256"],
                   "source_status": status, "start_line": anchor + 1, "end_line": anchor + 1,
                   "text": lines[anchor], "chunk_start_line": start + 1, "chunk_end_line": end,
                   "snippet_truncated": anchor != start or anchor + 1 != end}
            candidate = dict(result, results=result["results"] + [hit],
                             omitted_matches=count - len(result["results"]) - 1,
                             truncated=count > len(result["results"]) + 1)
            if _size(candidate) > budget:
                continue
            # Add adjacent exact lines when the shared response budget permits.
            for index in range(anchor + 1, min(end, anchor + 4)):
                expanded = dict(hit, text=hit["text"] + lines[index], end_line=index + 1,
                                snippet_truncated=anchor != start or index + 1 != end)
                extended = dict(candidate, results=result["results"] + [expanded])
                if _size(extended) > budget:
                    break
                hit, candidate = expanded, extended
            result = candidate
        if count and not result["results"]:
            raise ValueError("Matching original lines exceed budget_bytes; increase the budget")
        return result
