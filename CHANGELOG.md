# Changelog — babeldoc-offline-translate

**Current version: v2.6** (pure-pipe). This file is the authoritative version
history. The SKILL.md title, the "Version: v2.6" pointer near the top of
SKILL.md, and the `Pure-pipe flow (v2.N)` docstrings in `scripts/` /
`references/contract.md` must all match the latest entry below. If you bump the
version, update all four in one commit.

## v2.6 — pure-pipe (current)

- Agent translates BabelDOC's verbatim prompts (`prompts.jsonl`) directly. No
  `zh_texts.json` / `split_work.py` / `build_outputs.py` / tag-re-injection
  intermediate. `<bN>` tags are preserved byte-for-byte because they live
  inside the source text the agent sees (never proportionally re-injected).
- Added pure-pipe scripts:
  - `pipe_prep.py` — split `prompts.jsonl` into `chunk_N.json` (char-bounded,
    incremental by default, glossary term injection).
  - `pipe_merge.py` — merge `llm_chunk_*.json` into `llm_outputs.json` +
    `text_index.json` + `fallback_outputs.json`, with tag/id/coverage gates.
  - `pipe_status.py` — coverage / ETA reporting.
  - `replay_precheck.py` — predict replay coverage before merging.
  - `pipe_validate.py` — **pre-merge per-chunk gate**: checks every chunk's keys,
    batch length/id, and `<bN>` tag-sequence byte equality (exit 0 = clean).
  - `make_brief.py` — generate the per-book worker brief from
    `templates/worker_brief.md` (iron rules + self-check, placeholders filled).
- `run_translation.py replay` auto-aligns the page range from `run_stamp.json`
  when none is passed — removes the collect/replay page-range mismatch
  3-second abort.
- Dispatch protocol: ask the user before controlling parallelism; on 429 a
  subagent stops and reports the raw error instead of retrying blindly.

## v2.5 (superseded)

- Introduced the pure-pipe concept (verbatim-prompt capture, no source-keyed
  intermediate). Carried the `Pure-pipe flow (v2.5)` docstring in
  `pipe_prep.py` / `pipe_merge.py` / `references/contract.md` — relabeled to
  v2.6 in this release when the workflow was finalized.

## v2.0 (legacy — retained as fallback only)

- Source-keyed workflow: `export_texts.py` → fill `zh_texts.json` (or
  `split_work.py` slices) → `build_outputs.py` → replay. Includes
  tag-re-injection.
- **Retained strictly to salvage pre-v2.6 corpora. Never use on pure-pipe
  output** — the tag-re-injection step was the root cause of formula-tag
  corruption and is intentionally absent from v2.6.
