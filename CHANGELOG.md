# Changelog — babeldoc-offline-translate

**Current version: v2.8**. This file is the authoritative version
history. The SKILL.md title, the "Version: v2.8" pointer near the top of
SKILL.md, and the `Pure-pipe flow (v2.N)` docstrings in `scripts/` /
`references/contract.md` must all match the latest entry below. If you bump the
version, update all four in one commit.

## v2.8 — offline throughput + robustness hardening (current)

- **Decouple rate limiter from worker pool** (`run_translation.py`): the offline
  translator reads local JSON and never hits a remote API, yet babeldoc's global
  rate limiter sleeps on every `translate()` call (and `MeTranslator` passes
  `ignore_cache=True`, so the cache fast-path is skipped). The old
  `qps=qps, pool_max_workers=qps` coupling meant raising `qps` also resized the
  thread pool (`TranslationConfig` defaults `pool_max_workers` to `qps`), so a
  large `qps` would spawn that many threads. Now `set_translate_rate_limiter(1000)`
  (babeldoc has no "off" setting — `set_max_qps(0)` raises) makes the limiter a
  no-op, while `pool_max_workers` is a fixed `POOL_MAX_WORKERS=8`. Saves ~6 min of
  wall-clock per stage (collect and replay) on a ~2000-prompt book.
- **`render_check.py`**: output PNG filenames now include a short
  `sha1(pdf_path)[:6]` (`<stem>_<hash>_p<N>.png`) to prevent silently overwriting
  pages when comparing two same-stem PDFs rendered into one `--outdir`.
- **`_short_run` temp-dir key** (`run_translation.py`): `digest[:8]` → `digest[:16]`
  — keeps Windows 260-char path headroom (the tool exists to dodge the limit)
  while lowering collision probability; a full digest would re-introduce the
  overflow risk.
- **`build_outputs.py`**: added a legacy-path banner noting this is the v2.0
  source-keyed flow and pure-pipe (v2.6+) does not use it (the existing
  tiktoken-refusal exit is unchanged).
- **Skew-tolerance patch hardening** (`run_translation.py`): the babeldoc module
  import is now wrapped in `try/except` — on a future module rename the patch
  degrades to the native filter with a warning instead of crashing at startup.
  Removed the dead `-90°` snap branch (downstream `on_lt_char` only accepts +90°,
  so the branch could never rescue a glyph).
- **`pipe_validate.py`**: a work dir with zero chunks now fails (exit 2) instead of
  reporting a false `OK: all chunks present and clean`.

## v2.7 — skew-tolerant collect/replay

- `run_translation.py` now patches babeldoc's char-rotation filter
  (`il_creater` / `il_creater_active` bind `get_rotation_angle` independently;
  both are patched). babeldoc 0.6.x silently drops chars whose matrix is
  rotated >0.1° from the axes; warped scanned books carry ±0.5° per-line OCR
  skew, so whole paragraphs (page tops/bottoms) never reached `prompts.jsonl`
  and replay rendered the raw English image with zero missing logs. Tolerance
  band: `ME_SKEW_TOLERANCE_DEG` (default 5; `0` restores native filter).
  Near-horizontal snaps to 0°, near-vertical to ±90°; real rotations are kept.
  Found on The Heartfulness Way (2018): collect 1272 → 2034 prompts.
- Doc: SKILL.md preflight section documents the failure signature and the
  incremental re-collect path.

## v2.6 — pure-pipe

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
