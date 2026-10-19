"""Offline multilingual retrieval. Model preparation is explicit and never runs here.

E5 supplies semantic candidates from every verified memory, not just an FTS
shortlist. A separate SQLite cache can be rebuilt without changing project facts.
"""
from __future__ import annotations

from contextlib import contextmanager
from functools import lru_cache
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import time

# The harness forks git after encoding; fork-safe tokenizers avoid a noisy warning on stderr.
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")

MODEL_ID = "Xenova/multilingual-e5-small"
MODEL_REVISION = "761b726dd34fb83930e26aab4e9ac3899aa1fa78"
MODEL_FILES = ("onnx/model_quantized.onnx", "tokenizer.json")
INDEX_VERSION = 1
LEXICAL_VERSION = 2


class SemanticError(ValueError):
    """An explicit unavailable, stale or corrupt local semantic index."""


def _numpy():
    try:
        import numpy as np
        return np
    except ImportError as exc:
        raise SemanticError("Semantic search needs optional dependency numpy; install semantic extras") from exc


def _json_hash(value):
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _file_hash(path):
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def _normalize(vectors):
    np = _numpy()
    matrix = np.asarray(vectors, dtype=np.float32)
    if matrix.ndim != 2 or not np.isfinite(matrix).all():
        raise SemanticError("Encoder must return a finite two-dimensional embedding matrix")
    lengths = np.linalg.norm(matrix, axis=1, keepdims=True)
    if (lengths <= 0).any():
        raise SemanticError("Encoder returned a zero embedding")
    return np.asarray(matrix / lengths, dtype="<f4")


@lru_cache(maxsize=2)
def _stemmer(language):
    try:
        import snowballstemmer
    except ImportError as exc:
        raise SemanticError("Code-aware search needs optional dependency snowballstemmer==3.0.1") from exc
    return snowballstemmer.stemmer(language)


@lru_cache(maxsize=65536)
def _stem_token(token):
    if re.fullmatch(r"[a-z]+", token):
        return _stemmer("english").stemWord(token)
    if re.fullmatch(r"[а-яё]+", token):
        return _stemmer("russian").stemWord(token)
    return token


def normalize_code(text):
    """Generic code/prose tokenization; no project dictionaries or translations."""
    text = re.sub(r"([A-ZА-ЯЁ]+)([A-ZА-ЯЁ][a-zа-яё])", r"\1 \2", text)
    text = re.sub(r"([a-zа-яё0-9])([A-ZА-ЯЁ])", r"\1 \2", text)
    return " ".join(_stem_token(token.casefold()) for token in re.findall(r"[^\W_]+", text))


class OnnxEncoder:
    """Pinned E5 tokenizer and quantized ONNX weights, CPU only, no downloads."""

    model_name = MODEL_ID

    def __init__(self, model_dir, *, threads=2, batch_size=16):
        self.root = Path(model_dir).expanduser().resolve()
        self.batch_size = batch_size
        self.threads = threads
        self._session = None
        self._loaded_fingerprint = None
        self._tokenizer = None
        self._files = {}
        # Hash files before loading any executable model graph.
        self.fingerprint

    @property
    def fingerprint(self):
        hashes = {}
        for name in MODEL_FILES:
            path = self.root / name
            if not path.is_file():
                raise SemanticError(f"Missing local model file {path}; prepare the pinned model first")
            info = path.stat()
            stamp = (info.st_size, info.st_mtime_ns, info.st_ctime_ns, info.st_ino)
            previous = self._files.get(name)
            if not previous or previous[0] != stamp:
                self._files[name] = (stamp, _file_hash(path))
            hashes[name] = self._files[name][1]
        # Download manifests, when provided, bind the local files to their hashes.
        manifest_file = self.root / "manifest.json"
        if manifest_file.is_file():
            try:
                manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
                if manifest.get("revision", MODEL_REVISION) != MODEL_REVISION:
                    raise SemanticError("Local model manifest has a different pinned revision")
                declared = manifest.get("files", {})
                for name, checksum in hashes.items():
                    item = declared.get(name)
                    expected = item.get("sha256") if isinstance(item, dict) else item
                    if expected and expected != checksum:
                        raise SemanticError(f"Local model hash mismatch: {name}")
            except (OSError, json.JSONDecodeError, AttributeError) as exc:
                raise SemanticError("Invalid local model manifest") from exc
        return _json_hash({"model": MODEL_ID, "revision": MODEL_REVISION,
                           "files": hashes, "pooling": "attention-mean-l2", "max_tokens": 512,
                           "prefixes": ["query: ", "passage: "], "version": INDEX_VERSION})

    def _load(self):
        fingerprint = self.fingerprint
        if self._session is not None and self._loaded_fingerprint == fingerprint:
            return
        try:
            import onnxruntime as ort
            from tokenizers import Tokenizer
        except ImportError as exc:
            raise SemanticError("Semantic search needs onnxruntime and tokenizers; install semantic extras") from exc
        options = ort.SessionOptions()
        options.intra_op_num_threads = self.threads
        options.inter_op_num_threads = 1
        self._session = ort.InferenceSession(str(self.root / MODEL_FILES[0]), options,
                                            providers=["CPUExecutionProvider"])
        self._loaded_fingerprint = fingerprint
        self._tokenizer = Tokenizer.from_file(str(self.root / "tokenizer.json"))
        self._tokenizer.enable_truncation(max_length=512)
        padding = self._tokenizer.token_to_id("<pad>")
        self._tokenizer.enable_padding(pad_id=padding if padding is not None else 1,
                                       pad_token="<pad>")

    def encode(self, texts, *, kind="passage"):
        if kind not in ("query", "passage"):
            raise SemanticError("Encoder kind must be query or passage")
        np = _numpy()
        self._load()
        result = []
        inputs = {item.name for item in self._session.get_inputs()}
        for start in range(0, len(texts), self.batch_size):
            batch = self._tokenizer.encode_batch([f"{kind}: {text}" for text in texts[start:start + self.batch_size]])
            feed = {"input_ids": np.asarray([item.ids for item in batch], dtype=np.int64),
                    "attention_mask": np.asarray([item.attention_mask for item in batch], dtype=np.int64),
                    "token_type_ids": np.asarray([item.type_ids for item in batch], dtype=np.int64)}
            output = self._session.run(None, {key: value for key, value in feed.items() if key in inputs})[0]
            if output.ndim != 3:
                raise SemanticError("Pinned model must return token embeddings")
            mask = feed["attention_mask"][..., None].astype(np.float32)
            pooled = (output * mask).sum(axis=1) / np.maximum(mask.sum(axis=1), 1)
            result.append(_normalize(pooled))
        return np.concatenate(result) if result else np.empty((0, 384), dtype=np.float32)


class SemanticIndex:
    """Incremental embedding/code-stem cache with fixed reciprocal rank fusion.

    An injected encoder must expose fingerprint (string) and encode(texts, kind=).
    Each query is embedded anew. Tracked source files contribute at most two
    result records; unrelated external facts are not collapsed. The index never
    silently builds or falls back.
    """

    def __init__(self, store, model_dir=None, encoder=None):
        self.store = store
        self.db = store.root / "semantic.sqlite3"
        self.encoder = encoder
        self.model_dir = model_dir
        self._cache_stamp = None
        self._cache = None
        self._source_hashes = {}

    def _encoder(self):
        if self.encoder is None:
            directory = self.model_dir or os.environ.get("CONTEXT_HARNESS_MODEL_DIR")
            if not directory:
                config = self.store.root / "semantic-model.json"
                if config.is_file():
                    try:
                        directory = json.loads(config.read_text(encoding="utf-8"))["model_dir"]
                    except (OSError, ValueError, KeyError, TypeError) as exc:
                        raise SemanticError("Invalid semantic-model.json; set an explicit model directory") from exc
            if not directory:
                raise SemanticError("Semantic model directory is required; run semantic-build --model-dir PATH")
            self.encoder = OnnxEncoder(directory)
        return self.encoder

    @property
    def model_name(self):
        return getattr(self._encoder(), "model_name", "injected-local-encoder")

    def _rows(self):
        with self.store.connect() as con:
            return [dict(row) for row in con.execute("SELECT * FROM memories WHERE status='verified' ORDER BY id")]

    @staticmethod
    def _digest(row):
        # Source and status changes must invalidate inclusion, even if prose did not change.
        return _json_hash({key: row[key] for key in
                           ("id", "title", "content", "domain", "source", "source_hash", "status")})

    def _corpus_digest(self, rows):
        return _json_hash([(row["id"], self._digest(row)) for row in rows])

    @contextmanager
    def _connect(self):
        con = sqlite3.connect(self.db, timeout=15)
        con.row_factory = sqlite3.Row
        try:
            with con:
                yield con
        finally:
            con.close()

    def build(self):
        started = time.perf_counter()
        np = _numpy()
        encoder = self._encoder()
        fingerprint = str(encoder.fingerprint)
        rows = self._rows()
        old = {}
        # A corrupt old cache is not repaired implicitly: force an explicit removal.
        if self.db.exists():
            old_manifest, old_items, _ = self._read_cache()
            if old_manifest.get("encoder_fingerprint") == fingerprint:
                old = {item["id"]: item for item in old_items}
        changed = [row for row in rows if row["id"] not in old or old[row["id"]]["digest"] != self._digest(row)]
        embeddings = _normalize(encoder.encode([row["title"] + "\n" + row["content"] for row in changed], kind="passage")) if changed else None
        if embeddings is not None and len(embeddings) != len(changed):
            raise SemanticError("Encoder returned a different number of embeddings")
        dimensions = int(embeddings.shape[1]) if embeddings is not None else (len(next(iter(old.values()))["vector"]) // 4 if old else int(getattr(encoder, "dimensions", 384)))
        if old and any(len(item["vector"]) != dimensions * 4 for item in old.values()):
            raise SemanticError("Encoder dimensions changed without changing fingerprint")
        new_vectors = {row["id"]: embeddings[index].tobytes() for index, row in enumerate(changed)}
        lexical_rows = [(row["id"], row["domain"], normalize_code(row["title"]), normalize_code(row["content"]))
                        for row in rows]
        items = []
        for row in rows:
            vector = new_vectors[row["id"]] if row["id"] in new_vectors else old[row["id"]]["vector"]
            items.append((row["id"], self._digest(row), vector, hashlib.sha256(vector).hexdigest()))
        manifest = {"version": INDEX_VERSION, "model": self.model_name, "encoder_fingerprint": fingerprint,
                    "corpus_digest": self._corpus_digest(rows), "records": len(rows), "dimensions": dimensions,
                    "built_unix": time.time(), "fusion": "RRF-k60-code-stems-title5-exact-identifiers-max2-tracked-source",
                    "lexical_version": LEXICAL_VERSION,
                    "lexical_digest": _json_hash(lexical_rows)}
        # Ensure facts did not mutate during an expensive inference batch.
        if self._corpus_digest(self._rows()) != manifest["corpus_digest"]:
            raise SemanticError("Memory changed while building; rerun semantic-build")
        self.store.root.mkdir(parents=True, exist_ok=True)
        with self._connect() as con:
            con.executescript("CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT NOT NULL); "
                              "CREATE TABLE IF NOT EXISTS embeddings(id TEXT PRIMARY KEY,digest TEXT NOT NULL,vector BLOB NOT NULL,checksum TEXT NOT NULL); "
                              "CREATE VIRTUAL TABLE IF NOT EXISTS code_fts USING fts5(id UNINDEXED,domain UNINDEXED,title,content, tokenize='unicode61');")
            con.execute("DELETE FROM code_fts")
            con.executemany("INSERT INTO code_fts VALUES (?,?,?,?)", lexical_rows)
            con.execute("DELETE FROM embeddings")
            con.executemany("INSERT INTO embeddings VALUES (?,?,?,?)", items)
            con.execute("INSERT OR REPLACE INTO meta VALUES ('manifest',?)", (json.dumps(manifest),))
        self._cache_stamp = None
        self._cache = None
        return {**manifest, "embedded": len(changed), "reused": len(rows) - len(changed),
                "elapsed_ms": round((time.perf_counter() - started) * 1000, 3), "index": str(self.db)}

    def _read_cache(self):
        np = _numpy()
        if not self.db.is_file():
            raise SemanticError("Semantic index is not built; run semantic-build first")
        info = self.db.stat()
        stamp = (info.st_size, info.st_mtime_ns, info.st_ctime_ns, info.st_ino)
        if self._cache_stamp == stamp and self._cache is not None:
            return self._cache
        try:
            with self._connect() as con:
                raw = con.execute("SELECT value FROM meta WHERE key='manifest'").fetchone()
                manifest = json.loads(raw[0])
                items = [dict(row) for row in con.execute("SELECT * FROM embeddings ORDER BY id")]
                if manifest.get("lexical_version") == LEXICAL_VERSION:
                    lexical_rows = [tuple(row) for row in con.execute("SELECT id,domain,title,content FROM code_fts ORDER BY id")]
                    if _json_hash(lexical_rows) != manifest.get("lexical_digest"):
                        raise SemanticError("Code-aware lexical cache is corrupt")
            if manifest["version"] != INDEX_VERSION or len(items) != manifest["records"]:
                raise SemanticError("Semantic cache version or row count is invalid")
            dimensions = manifest["dimensions"]
            if not isinstance(dimensions, int) or not 1 <= dimensions <= 65536:
                raise SemanticError("Semantic cache dimensions are invalid")
            if not isinstance(manifest.get("encoder_fingerprint"), str) or not isinstance(manifest.get("corpus_digest"), str):
                raise SemanticError("Semantic cache manifest is invalid")
            vectors = []
            for item in items:
                blob = item["vector"]
                if len(blob) != dimensions * 4 or hashlib.sha256(blob).hexdigest() != item["checksum"]:
                    raise SemanticError("Semantic cache vector is corrupt")
                vector = np.frombuffer(blob, dtype="<f4")
                if not np.isfinite(vector).all() or not np.isclose(np.linalg.norm(vector), 1, atol=1e-4):
                    raise SemanticError("Semantic cache contains a non-normalized vector")
                vectors.append(vector)
            matrix = np.stack(vectors) if vectors else np.empty((0, dimensions), dtype=np.float32)
        except (sqlite3.Error, json.JSONDecodeError, TypeError, KeyError, IndexError) as exc:
            raise SemanticError("Semantic cache is corrupt; remove it and explicitly rebuild") from exc
        self._cache_stamp = stamp
        self._cache = (manifest, items, matrix)
        return self._cache

    def status(self):
        manifest, _, _ = self._read_cache()
        return {**manifest, "current": manifest["corpus_digest"] == self._corpus_digest(self._rows())
                and manifest["encoder_fingerprint"] == str(self._encoder().fingerprint)
                and manifest.get("lexical_version") == LEXICAL_VERSION, "index": str(self.db)}

    def lexical_search(self, query, domain=None, limit=60):
        tokens = list(dict.fromkeys(normalize_code(query).split()))[:32]
        if not tokens:
            return []
        match = " OR ".join('"' + token.replace('"', '""') + '"' for token in tokens)
        sql = "SELECT id,bm25(code_fts,0,0,5,1) AS lexical_rank FROM code_fts WHERE code_fts MATCH ?"
        args = [match]
        if domain:
            sql += " AND domain IN (?, 'general')"
            args.append(domain)
        sql += " ORDER BY lexical_rank,id LIMIT ?"
        args.append(limit)
        try:
            with self._connect() as con:
                return [dict(row) for row in con.execute(sql, args)]
        except sqlite3.Error as exc:
            raise SemanticError("Code-aware lexical index unavailable; run semantic-build") from exc

    def search(self, query, domain=None, limit=12, lexical=None, variants=None):
        if variants is None:
            variants = []
        if not isinstance(variants, (list, tuple)) or len(variants) > 2:
            raise SemanticError("Query variants must be a list of at most two nonempty strings")
        if any(not isinstance(value, str) or not value.strip() or len(value) > 500 for value in variants):
            raise SemanticError("Each query variant must be nonempty text of at most 500 characters")
        variants = [value.strip() for value in variants]
        if not isinstance(query, str) or not query.strip():
            return []
        if not isinstance(limit, int) or limit < 1:
            raise SemanticError("Semantic search limit must be positive")
        encoder = self._encoder()
        manifest, items, matrix = self._read_cache()
        if manifest.get("lexical_version") != LEXICAL_VERSION:
            raise SemanticError("Code-aware index is not built; run semantic-build (existing embeddings are reusable)")
        if manifest["encoder_fingerprint"] != str(encoder.fingerprint):
            raise SemanticError("Semantic model changed; explicitly rebuild the index")
        rows = self._rows()
        if manifest["corpus_digest"] != self._corpus_digest(rows):
            raise SemanticError("Semantic index is stale after memory changes; run semantic-build")
        by_id = {row["id"]: row for row in rows}
        source_fresh = {}
        project = self.store.project
        eligible = []
        for index, item in enumerate(items):
            row = by_id[item["id"]]
            if domain and row["domain"] not in (domain, "general"):
                continue
            if row["source_hash"]:
                source = re.sub(r":\d+$", "", row["source"])
                # Many chunks share a file. Stat each source per query and hash
                # only when bytes could have changed, including replacement/ctime.
                if source not in source_fresh:
                    path = Path(source)
                    path = (project / path).resolve() if not path.is_absolute() else path.resolve()
                    if path.is_relative_to(project) and path.is_file():
                        info = path.stat()
                        stamp = (info.st_size, info.st_mtime_ns, info.st_ctime_ns, info.st_ino)
                        cached = self._source_hashes.get(str(path))
                        if not cached or cached[0] != stamp:
                            cached = (stamp, _file_hash(path))
                            self._source_hashes[str(path)] = cached
                        source_fresh[source] = cached[1]
                    else:
                        source_fresh[source] = None
                if source_fresh[source] != row["source_hash"]:
                    continue
            eligible.append(index)
        # Variants are explicit caller-provided data, never generated from a
        # hidden dictionary or retrieved answer. All queries are embedded anew.
        queries = [query] + variants
        query_vectors = _normalize(encoder.encode(queries, kind="query"))
        if query_vectors.shape != (len(queries), manifest["dimensions"]):
            raise SemanticError("Query encoder dimensions do not match the built index")
        if not eligible:
            return []
        # Keep the original single-query math path for identical default behavior.
        if not variants:
            query_scores = [matrix[eligible] @ query_vectors[0]]
        else:
            query_scores = (matrix[eligible] @ query_vectors.T).T
        window = max(60, limit * 5)
        semantic_rank = {}
        score_by_id = {}
        for scores in query_scores:
            semantic = sorted(zip(eligible, scores.tolist()), key=lambda pair: (-pair[1], items[pair[0]]["id"]))
            for rank, (index, score) in enumerate(semantic, 1):
                mid = items[index]["id"]
                score_by_id[mid] = max(score_by_id.get(mid, float("-inf")), score)
                if rank <= window:
                    semantic_rank[mid] = min(semantic_rank.get(mid, rank), rank)
        eligible_ids = set(score_by_id)
        lexical_rank = {}
        for query_number, query_text in enumerate(queries):
            lex = lexical if query_number == 0 and lexical is not None else self.lexical_search(query_text, domain, window)
            lex = [row for row in lex if row["id"] in eligible_ids][:window]
            for rank, row in enumerate(lex, 1):
                mid = row["id"]
                lexical_rank[mid] = min(lexical_rank.get(mid, rank), rank)
        ids = set(semantic_rank) | set(lexical_rank)
        identifiers = [token for token in re.findall(r"[\w.]+", query)
                       if "_" in token or "." in token or re.search(r"[a-z][A-Z]", token)]
        def exact(row):
            text = row["title"] + "\n" + row["content"]
            return any(re.search(r"(?<!\w)" + re.escape(token) + r"(?!\w)", text) for token in identifiers)
        def fusion(mid):
            return (1 / (60 + semantic_rank[mid]) if mid in semantic_rank else 0) + (1 / (60 + lexical_rank[mid]) if mid in lexical_rank else 0)
        ordered = sorted(ids, key=lambda mid: (-int(exact(by_id[mid])), -fusion(mid), -score_by_id[mid], mid))
        selected = []
        tracked_counts = {}
        for mid in ordered:
            row = by_id[mid]
            if row["source_hash"]:
                source = re.sub(r":\d+$", "", row["source"])
                path = Path(source)
                path = (project / path).resolve() if not path.is_absolute() else path.resolve()
                key = str(path)
                if tracked_counts.get(key, 0) >= 2:
                    continue
                tracked_counts[key] = tracked_counts.get(key, 0) + 1
            selected.append({**row, "semantic_score": score_by_id[mid], "hybrid_score": fusion(mid)})
            if len(selected) == limit:
                break
        return selected
