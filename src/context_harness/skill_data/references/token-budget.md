# Bounded context and recovery

The CLI uses UTF-8 **byte budgets**, measured on its complete pretty-printed JSON including metadata and newline. They are deterministic caps, not estimated model-token counts. `--compact` emits the same data with less JSON whitespace; caps still use the larger pretty form. Exact titles/paths/IDs, goals, acceptance, ownership and protocol are never paraphrased to force a fit.

## Where the tokens go

Each model call re-sends the whole conversation, so total input grows with calls × context size, roughly quadratically in the number of turns. Shrinking one payload saves little; avoiding a call saves that call and its share of every later call. In one traced v0.4 session (internal, unpublished) about half of the tokens were spent after the full check was already green. Hence: one `start` call, budgets on `run`, a stop rule after green, and no in-loop protocol calls.

## Memory delivery

`start`, `assign` and `search` default to the `pointers` view: code records become `{path, lines, symbols}` pointing at the *current* file, and facts/lessons are delivered complete. The agent never reads the same bytes twice through the harness. `inline` adds current text of each code range (at most 60 lines each). The legacy `index` view plus `memory-read ID` and `full` remain for coordinators.

`--backend auto` (default) uses lexical FTS unless the query and memory use different scripts (e.g. Russian symptom, English code) or query variants are given, and the semantic index is built. Corpus size alone does not switch to hybrid: in one internal replay an English query that named a file ranked that file 3rd lexically and 8th with hybrid. The result states the choice in `retrieval_note`; a wanted-but-missing semantic index is reported, never hidden. Explicit `--backend hybrid` still fails without an index.

File and identifier names in the query (`use-paged-api`, `useDismiss`, `apply_items`) pin records whose source path or title contains them. In the `pointers`/`inline` views code pointers are then delivered only when confident: identifier matches, or records covering at least half of the query's rare-word weight (words present in at most 2% of memory). Everything else is dropped and counted in `context_dropped_irrelevant`; facts and lessons are always kept. Several chunks of one file become one pointer with merged line ranges. An empty context is a valid answer: the agent's own search is cheap, a wrong pointer is not.

Byte budgets (`--budget-bytes`) are UTF-8 caps on the pretty-printed JSON including metadata, not model-token counts. Mandatory task criteria and ownership must fit intact; otherwise creation fails.

## Test and check output

`run [--full] -- <command>` writes the complete combined stdout/stderr to `logs/<task>/NNN-*.log` in state and returns:

- while it runs, a heartbeat on stderr every 15 s (`CONTEXT_HARNESS_HEARTBEAT`), or MCP `notifications/progress` when the client sends a progress token, so agents whose shell returns early wait instead of polling (in one traced run an agent spent 7 extra calls polling a silent 75-second check);
- short output (≤1500 bytes) unchanged, with the real exit code;
- otherwise a digest: counts, failing test names, the first failure excerpt, the last lines and the log path. Grep or read line ranges of the log instead of re-running.

Parsers cover vitest, jest, pytest, unittest, go test and cargo; anything else still gets exit code, first error-looking excerpt and tail. A digest never decides pass/fail; the exit code does.

Budgets per task and agent (defaults): 12 test/check runs, 1 run after the full check is green, 3 failures of the same test, 2 overrides. A refused run is not executed (CLI exit 75) and returns its reasons; an identical command on an unchanged git working tree returns the previous result instead of running again. `npx tool` without a local install is refused rather than reaching the network. `--override "new hypothesis"` runs anyway, is counted and recorded.

The explicit output vault (`output-ingest`, `output-search`, `output-read`) remains for logs produced outside `run`. Short logs are cheaper read directly: in one internal replay a 2060-byte failure log cost 723 proxy tokens directly versus 983 through the vault.

## Reporting and bounded refinement

Use structured brief statements, preserving qualifiers and failed checks. Do not remove words mechanically from code, user requirements, evidence or ordinary user-facing explanations. Avoid repeated already-known prose; read raw evidence when a short card is insufficient.

`checkpoint` contains current task, every acceptance criterion, latest reports and verification; it does not summarize an entire conversation or recover hidden reasoning. Full report/event history remains on demand. Required oversized content raises an error.

`refine-plan` is a PPC-inspired coordination primitive, **not native Harness²**. A failed independent verification and unchanged report version are required. It records up to one plan by default (explicit maximum two), with a declared token cap. Actual process/model/token enforcement stays with the coordinator. There is no automatic winner selection or automatic reward training. A candidate must pass the original quality gate before a lesson can even be proposed; `memory-verify` remains separate.

## Design sources

Independently implemented patterns, not installed upstream runtimes:

- Caveman: brief protocol/output, retain byte-exact original and recovery.
- claude-mem: index first, selected details second.
- context-mode: keep large tool results outside the conversation; search/addressed read.
- XERJ: project/domain scoping, top-k and selected fields; retain existing E5 semantic search.
- Harness²: propose/probe from observed failures, independent acceptance and scoped lessons.

Upstream percentage claims are not our measurements. A context-mode source analysis informed design; its ELv2 implementation is not vendored or installed.
