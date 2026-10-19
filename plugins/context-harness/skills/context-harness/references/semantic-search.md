# Local multilingual retrieval

Hybrid search embeds verified memory, including Russian and English, with pinned quantized `Xenova/multilingual-e5-small` revision `761b726dd34fb83930e26aab4e9ac3899aa1fa78`. The encoder uses query/passage prefixes, 512-token truncation, masked mean pooling and normalized 384-dimensional vectors. Long records can lose trailing text; split evidence into focused facts.

## Prepare once

Choose an explicit runtime/model directory outside source-controlled project files. Use a supported Python runtime (the measured run used Python 3.11). Replace placeholder paths below with the actual skill, state and runtime paths.

```sh
python3.11 -m venv /absolute/runtime/venv
/absolute/runtime/venv/bin/pip install 'numpy>=1.26,<3' 'tokenizers>=0.21,<0.24' 'onnxruntime>=1.23,<1.24' 'snowballstemmer==3.0.1'
/absolute/runtime/venv/bin/python /absolute/skill/scripts/prepare_semantic.py --output /absolute/runtime/model
/absolute/runtime/venv/bin/python /absolute/skill/scripts/ch.py --state /absolute/project/.context-harness semantic-build --model-dir /absolute/runtime/model
```

The explicit downloader reads only public pinned weights/tokenizer (about129 MiB total) and verifies expected sizes and SHA-256. It retains interrupted downloads for resumption. Model weights are separate from the skill archive. Neither index building nor search downloads or calls an inference API.

`semantic-build` stores the model directory in `semantic-model.json` inside project state. `CONTEXT_HARNESS_MODEL_DIR` is an optional override. Run hybrid commands with the prepared interpreter; ordinary lexical commands retain the standard-library-only runtime.

## Search and assignments

With `--backend auto` (default) hybrid is used only when it is likely to help and the index exists; see token-budget.md. The agent prepares natural-language queries using the same uniform rule used in the benchmark: faithful English translation plus one English synonym paraphrase, retaining every condition. It sees the query before retrieval and does not derive variants from expected answers. Exact code identifiers keep no variants. The planning happens in the agent; the local CLI is an explicit consumer of the strings. An API key for an additional provider is not needed for this step, but ordinary agent reasoning time/context still applies.

```sh
/absolute/runtime/venv/bin/python /absolute/skill/scripts/ch.py --state /absolute/project/.context-harness search 'пауза опроса в скрытой вкладке' --domain frontend --backend hybrid
/absolute/runtime/venv/bin/python /absolute/skill/scripts/ch.py --state /absolute/project/.context-harness search 'возврат товара' --backend decisions --retrieval hybrid
```

Pass planned translations/paraphrases as up to two `--variant TEXT` options. Both `search` and `assign` accept them. Original and variants are encoded in one local batch every request; the best semantic rank and best code-aware lexical rank per record feed the same fixed RRF. Original-query exact identifiers retain priority. The CLI never silently rewrites a query or calls a planner itself.

`assign` accepts the same `--backend hybrid` or `--backend decisions --retrieval hybrid` options. The second form still requires the user's selected live API credentials; local embeddings and live ranking are separate steps and separate model fields in results.

## Invariants and limits

- `semantic.sqlite3` contains private vectors and fingerprints, separate from factual memory. Build reuses unchanged vectors; adding/verifying or editing a memory invalidates retrieval until an explicit rebuild.
- Only verified memory is indexed; domain filtering includes `general`. Changed/deleted local source files are excluded at search time. Source hashes are cached per file metadata stamp and rechecked on changes. Model/content/index corruption is an explicit error.
- Dense similarity scans all eligible records. A separate derived FTS5 index splits CamelCase/acronym/snake/dotted code names and applies English/Russian Snowball stems with title weight5/content weight1. It fuses up to60 lexical and semantic candidates using reciprocal ranks (k=60). Exact underscore/CamelCase/dotted identifiers receive priority. At most2 records per tracked local source file enter the result; external facts remain uncapped. There is no query-specific synonym dictionary, answer-key mapping or training step.
- The first raw E5+FTS experiment did not improve the original12-query results. The released pipeline adds code/morphological normalization and source diversity. Report the combined change; do not attribute its gains solely to embeddings. Old vectors can be reused when rebuilding the new derived lexical schema.
- Code/morphological normalization alone also left the original top5 hit count unchanged. The final evaluated workflow adds explicit Codex query planning. Keep the unplanned hybrid and planned workflow results separate. Local benchmark timings exclude generation of cached benchmark query plans; ordinary use must generate plans for new queries.
- Each query is encoded anew. A persistent process can reuse the loaded model and vectors; separate CLI invocations pay loading cost. Warm search latency and model startup are different measurements.
- This is a retrieval aid, not a confidence guarantee. It can return irrelevant results even when no exact words match. Dense search is linear, not an approximate-nearest-neighbor index; large-scale performance needs separate measurement.
- Use one explicit state directory per project. The benchmark-only source importer is separate from normal curated memory; no automatic whole-repository ingestion is added by this feature.
