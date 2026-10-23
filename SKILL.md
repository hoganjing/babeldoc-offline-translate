---
name: babeldoc-offline-translate
description: >-
  Offline collect→translate→replay pipeline that makes the agent itself the
  LLM translation backend for BabelDOC / pdf2zh PDF translation (layout
  preserved, EN→ZH by default, any language pair via env). v2.6 ("pure-pipe"):
  feed BabelDOC's verbatim prompts straight to the agent, preserve <bN> tags
  byte-for-byte, merge responses directly into llm_outputs.json — no
  source-keyed intermediate and no tag re-injection step. Use when
  translating a PDF with babeldoc or pdf2zh without a live OpenAI/Anthropic
  API key, when the user says "翻译 PDF", "translate this PDF with babeldoc",
  "pdf2zh offline", "做离线翻译", "collect/replay 翻译", or when a BabelDOC
  run must serve pre-translated text through a custom BaseTranslator.
---

# BabelDOC Offline Translator (agent-as-LLM) — v2.6 pure-pipe

> **Version: v2.6** (pure-pipe). The authoritative version history lives in
> `CHANGELOG.md` — keep this line, the title, and that file in sync.

Turn the agent into BabelDOC's translation API. No network calls, no API
keys: a **collect** run dumps every prompt, the agent translates offline, a
**replay** run serves the pre-translated text and writes the final PDFs.

## v2.6 in one paragraph

BabelDOC's own LLM path sends one prompt per batch (a JSON array of
paragraphs) plus a single-paragraph fallback. v2.6 captures those prompts
verbatim (`prompts.jsonl`), hands them to the agent, and the agent returns
translations that keep every `<bN>` tag byte-for-byte. `pipe_merge.py` then
builds `llm_outputs.json` directly from the agent's replies. There is **no**
`zh_texts.json` / `split_work.py` / `build_outputs.py` / tag-re-injection
stage — which is exactly the stage where earlier workflows could
proportionally re-inject and misalign formula tags. The tags live inside the
text the agent sees, so they can only be preserved or dropped, never
misplaced.

## When to use

- Translate a PDF (layout preserved) with babeldoc/pdf2zh but there is no
  usable LLM API key, or the user explicitly wants the agent to translate.
- Resume/extend a translation where prompts were already collected.

Do NOT use for: one-off text translation with no PDF involved; live-API
babeldoc runs (`babeldoc` CLI with `--openai` etc. works fine as-is).

## Prerequisites

- Python with `babeldoc` 0.6.x installed: `pip install babeldoc "bitstring<5"`
  (`bitstring>=5` breaks babeldoc — pin it). `tiktoken` required only by the
  optional v2.0 source-keyed path (`build_outputs.py`); the pure-pipe path
  needs neither tiktoken nor `export_texts.py`.
- `preflight.py` and `render_check.py` need PyMuPDF (`pip install pymupdf`;
  babeldoc usually installs it). They exit 2 with an install hint when `fitz`
  is unavailable; the collect/translate/replay path does not need it.
- On Windows set `PYTHONUTF8=1` in the session before running anything
  (prints include CJK source text; cp936 consoles crash otherwise).
- Scripts live in `<skill>/scripts/`: `me_translator.py`, `run_translation.py`
  (collect/replay core), `pipe_prep.py`, `pipe_merge.py`, `pipe_validate.py`
  (pre-merge per-chunk gate), `replay_precheck.py` (pure-pipe),
  `pipe_status.py` (coverage/ETA), `make_brief.py` (generate the per-book worker
  brief from `templates/worker_brief.md`), `selftest.py` (offline checks),
  the PDF utilities `preflight.py` / `render_check.py`, and the retained v2.0
  helpers `export_texts.py`, `build_outputs.py`, `split_work.py`.

> **Legacy v2.0 helpers are fallback-only.** `export_texts.py`, `build_outputs.py`,
> `split_work.py`, and any project-local legacy tools such as `*reinject*.py`
> belong to the *old* source-keyed workflow. The current pure-pipe (v2.6) path
> (collect → pipe_prep → pipe_merge → replay) does **not** use them and must
> **never** run tag-re-injection on pure-pipe output — that step was the root
> cause of formula-tag corruption. Use them only to salvage pre-v2.6 corpora.

## File contract (work dir `./me_work`, override with `ME_WORK_DIR`)

| File | Shape | Written by |
|---|---|---|
| `prompts.jsonl` | one record per prompt: `{"kind","key","prompt"\|"source"}` | collect |
| `run_stamp.json` | `{pages, sha256, input, lang_in, lang_out}` | collect |
| `chunk_N.json` | `{"chunk","instruction","output","keys","records":[...]}` for subagents | `pipe_prep.py` |
| `llm_chunk_N.json` | `{"<key>": <value>}` — one per subagent | subagent |
| `llm_outputs.json` | keyed `b:`/`s:` map (batch value = JSON string) | `pipe_merge.py` |
| `text_index.json` | `{sha256(source text): translation}` — exact-text reuse | `pipe_merge.py` |
| `coverage_missing.jsonl` | text-addressed gaps `{"key":"t:..","kind":"text","source"}` | `pipe_merge.py` |
| `retry.jsonl` | segments still defective (tag/id/empty/gap; text-addressed) | `pipe_merge.py` |
| `coverage_report.json` | coverage counts + problems (machine-readable) | `pipe_merge.py` |
| `content_warnings.jsonl` | suspected untranslated / too short / too long segments | `pipe_merge.py` |
| `glossary_warnings.jsonl` | glossary term in source but missing from the output (advisory) | `pipe_merge.py` |
| `fallback_outputs.json` | `{sha256(source): translation}` for **every** known text; covers the plain path | `pipe_merge.py` |
| `glossary.json` | optional `{"source term": "target term"}`; injected per chunk | user / `pipe_prep.py --glossary` |
| `src_texts.json` / `zh_texts.json` / `zh_slice_*.json` | v2.0 source-keyed path only | v2.0 helpers |

Translations are keyed by BabelDOC's own cache keys, never by list index —
misalignment is structurally impossible.

## Workflow (strict order)

Run everything from ONE working directory. `collect` and `replay` must use the
same PDF content, page range, and source/target languages (enforced by
`run_stamp.json`).

### 0. Preflight

```bash
export PYTHONUTF8=1   # Windows PowerShell: $env:PYTHONUTF8='1'
python <skill>/scripts/preflight.py <input.pdf>
```

Read the report first: zero-text pages (won't translate), image-dominant
pages (`ME_OCR_WORKAROUND=1` needed if collect raises ScannedPDFError), long
path warning, babeldoc version warning. Long input paths are handled
automatically: collect/replay copy the PDF to `%TEMP%\me_short\<sha8>\`, run
with short input/output/working dirs, then copy the rendered PDFs back to
`out_<mode>/`. A short copy of the PDF is still a good precaution for extreme
paths.

### 1. Collect

```bash
python <skill>/scripts/run_translation.py collect <input.pdf> [page_range]
```

Success: `[me] prompts=N ... unparsed=0`.
- `prompts=0` → wrong range / image-only PDF: investigate before proceeding.
- `ScannedPDFError` → rerun with `ME_OCR_WORKAROUND=1` (collect AND replay).
- Re-collect on a **different** PDF, a different `ME_LANG_IN`/`ME_LANG_OUT`, or
  a page range that is not a strict extension automatically archives the
  previous run's artifacts (prompts, outputs, indexes, chunks, retry/coverage
  reports, chunk metadata, and `out_collect`/`out_replay`) to
  `me_work/archive/<hash>_<ts>/` and starts clean.
- Re-collect on the **same** PDF with the same languages and an unchanged or
  extended page range appends — earlier prompts are kept (incremental), and
  prompt/fallback keys already in the logs are not written twice. Shrinking or
  arbitrarily changing the range archives instead, so stale out-of-range
  prompts cannot leak into the next merge.

### 2. Translate offline — PURE-PIPE (recommended, v2.6)

```bash
# 2a. split prompts into chunks for parallel subagents
python <skill>/scripts/pipe_prep.py --chunks 8 [--max-chars 32000] [--seed me_work/zh_texts.json]
#     optional terminology: --glossary me_work/glossary.json (auto-detected if present)
#     --no-glossary disables auto-detection
#     incremental by default: covered sources are skipped; --all re-chunks everything
# 2b. generate the per-book worker brief ONCE (template -> filled brief), then
#     hand each me_work/chunk_N.json to one parallel subagent.
python <skill>/scripts/make_brief.py --work me_work --book "<Title (Year)>" \
    --meta "<authors>, <year>, <pages> pages, <domain>" \
    --terms glossary_subset.txt   # optional: one 'src→tgt' per line
#     The brief (me_work/WORKER_BRIEF.md) carries the iron rules + self-check;
#     ship it to every subagent instead of hand-writing the rules per book.
#     DISPATCH PROTOCOL (parallelism): ASK the user before dispatching — see the
#     'Dispatch protocol' note below. If they decline, the main agent judges the
#     concurrency on the fly (call as many as it sees fit, not forced serial).
#     On 429 a subagent stops + reports; main agent waits for quota reset + resumes.
# 2c. after every chunk is answered, run the per-chunk gate BEFORE merging:
python <skill>/scripts/pipe_validate.py --work me_work
#     exit 0 = all chunks present + clean (tags/ids/keys OK); exit 3 = do NOT merge.
# 2d. merge all replies + validate tags/ids + close coverage gaps
python <skill>/scripts/pipe_merge.py
```

**Dispatch protocol (parallelism).** Before dispatching the chunk subagents,
ask the user whether they want to control the parallel count. If they decline,
the main agent judges the concurrency on the fly — call as many parallel
subagents as it sees fit for the situation, not forced into a single serial
worker. If they specify a number N, honor it (1 = fully serial, 3/5 = that
many concurrent Agent dispatches). Regardless of the path, a subagent that hits
a 429 / rate-limit must STOP and report the raw error verbatim; the main agent
then pauses, waits for the quota to reset, and resumes dispatch — never retry
blindly.

`--chunks` is a **target**, `--max-chars` a **hard cap** (default 32000).
Chunks are packed by source characters, not record count: batch records range
from a few hundred bytes to several KB, and equal-count chunks of a 400-page
book can exceed 300 KB each and time out. The script prints actual chunk sizes;
if a single record exceeds the cap it is kept whole with a warning (records are
never split across files — one batch key is answered once).

Prep is **incremental by default**: a single is skipped when its exact source
already has a non-empty translation in `text_index.json`; a batch is skipped
only when every item is covered. `--all` forces a full re-chunk. This is how a
re-run after an extended collect or a quality pass avoids paying twice.

**Subagent contract** (also embedded in every chunk file):
- `batch` record: translate each `items[].input` to Chinese, keep every
  `<bN>` tag byte-for-byte (same sequence, same relative position), return
  `value` = `[{"id": N, "output": "<zh with <bN>>"}, ...]` — a JSON array
  with **every** id, unchanged.
- `single` record: translate `source`, keep `<bN>` tags, return `value` = the
  plain string.
- Never invent keys — each record already carries its exact key.
- Write the file named by the chunk's `output` field **before** replying; do
  not echo translations into chat.

**Glossary (optional).** If `<work>/glossary.json` exists (or `--glossary PATH`),
prep injects only the terms that actually occur in each chunk — word-boundary,
case-insensitive, longest/most frequent first, max 60 per chunk — as an extra
numbered rule in that chunk's instruction. It never touches `prompts.jsonl` or
the keys, so BabelDOC's batching/layout are unchanged and a run without a
glossary behaves identically. The run prints how many terms were injected into
how many chunks; `--no-glossary` ignores the file.
- `--seed` (optional, re-runs only): attaches a prior `zh_texts.json` entry as
  a quality reference; the agent must still re-place tags per the rules.

`pipe_merge.py` prints key/text coverage, `TAG-SEQUENCE PROBLEMS`, and
`COVERAGE GAPS`, then writes `llm_outputs.json` + `text_index.json` +
`fallback_outputs.json`. It exits `3` when anything is incomplete (missing ids,
empty translations, tag damage, retry values whose tags are invalid,
conflicting duplicate keys, or real coverage gaps) — a non-zero exit means
**do not replay yet**.

It also runs content-level checks that tags cannot catch and prints
`CONTENT WARNINGS` (written to `content_warnings.jsonl`): a segment identical to
its source (more than 10 words), no target-script characters in a long segment,
or a target text under 10% / over 4x the source length. Only prose is judged
(≥6 lowercase words after tag removal), so formulas, names, city lists and
bibliography entries are skipped. These are advisory (replay never sees them)
unless you pass `--strict`, which makes them exit 3. Review the file; a real hit
means that paragraph would otherwise ship in English.

When `<work>/glossary.json` (or `--glossary PATH`) is present, merge also checks
adherence: a source segment containing a glossary term must contain its target
term. Misses are printed as `GLOSSARY WARNINGS` and written to
`glossary_warnings.jsonl`. This is advisory (never blocks replay);
`--no-glossary` skips the check.

Coverage gaps are text-level, not key-level: the same paragraph can appear
under a `b:` key and several `s:` keys, and BabelDOC's batch keys are not
stable across runs. Merge reuses a translation whenever the **source text is
byte-identical** (tags included) and never re-maps tags. Remaining gaps are
written to `coverage_missing.jsonl`; close them with:

```bash
python <skill>/scripts/pipe_prep.py --from-missing coverage_missing.jsonl
# translate chunk_fix_*.json -> llm_chunk_fix_*.json, then merge again
python <skill>/scripts/pipe_merge.py
```

If two chunk files translate the same key differently, merge fails by default;
`--conflict last` keeps the newest file's value (use deliberately, e.g. after
a retry round). The conflict report names the file actually kept and every file
whose value was overwritten, including when a third file wins.
`--allow-gaps` exists for debugging only.

Whatever is still defective is also written to `retry.jsonl` with the exact
source text and a reason (tag mismatch, empty/missing id, unusable value,
coverage gap). `--retry-content` adds content warnings; `--import-missing` folds
replay's `missing.jsonl` / `missing_fallback.jsonl` in. Close the loop without
hand-editing chunk files:

```bash
python <skill>/scripts/pipe_prep.py --from-retry retry.jsonl
# translate chunk_retry_*.json -> llm_chunk_retry_*.json
python <skill>/scripts/pipe_merge.py
```

Retry values are text-addressed (`t:<sha>`) and **override** the previous bad
value, including a single item inside a batch key. A retry value whose tag
sequence does not match its source is never applied: merge prints
`RETRY VALUES WITH BAD TAGS`, lists it in `retry.jsonl`, and exits 3 — even
when the normal batch value is fine. When nothing is left,
`retry.jsonl` is removed and merge exits 0. A retry/fix file loses to a normal
`llm_chunk_*.json` that covers the same source and is newer (merge prints
`ignored N stale text-fix value(s)` and reports `retry_ignored`), so a deliberate
re-translation wins automatically. Regenerate the retry file after the normal
chunk if the retry value should win instead.

Run `python <skill>/scripts/selftest.py [--work <work>]` to verify the scripts
end-to-end without a PDF (retry loop, warnings, incremental, preflight) and
optionally audit a real work dir.

### 2-alt. Translate offline — source-keyed (v2.0, kept for small jobs / incremental)

```bash
python <skill>/scripts/export_texts.py      # src_texts.json + zh_texts.json
# fill every value in zh_texts.json (edit, or split_work.py + parallel slices)
python <skill>/scripts/build_outputs.py     # validates + writes llm_outputs.json
```

Rules: output ≠ input when input > 10 tokens; token ratio (0.3, 3); `<bN>`
tags byte-identical; paragraph count equal; terminology via `glossary.json`.
`tiktoken` is mandatory here (build fails loudly without it). This path is
fully supported but is the historical route with the larger moving surface —
prefer the pure-pipe path for new work.

### 3. Replay

```bash
python <skill>/scripts/run_translation.py replay <input.pdf> [page_range] [--log-file replay.log]
```

Refuses on any collect/replay mismatch (pages, PDF content sha, source language,
target language) — fix by re-running collect or replay with the collected
values. `[page_range]` is now **optional**: when omitted, replay auto-uses the
range recorded in `run_stamp.json` at collect time, so you can no longer trigger
the 3-second "page range mismatch" abort by forgetting the range (this used to
bite when collect recorded an explicit range like `1-910`). Passing it explicitly
still works and must match collect.

Before loading the layout model, replay runs a **coverage preflight** over
`prompts.jsonl` + `llm_outputs.json` + `text_index.json`:

```
[me] preflight: prompts=2852 keyed=2852 text-index=0 missing=0 (texts indexed=2504)
```

`missing > 0` aborts with exit 3 **before any rendering**, listing the affected
keys and source snippets and the merge/`--from-missing` commands to close them.
The same check covers BabelDOC's plain `do_translate` path: sources recorded in
`fallback_src.jsonl` must resolve in `fallback_outputs.json` or `text_index.json`
(the report adds a `plain-path sources=... uncovered=...` line when any exist).
`--allow-misses` replays anyway; `--precheck-only` only reports (adding
`--allow-misses --precheck-only` gives a clean exit 0 after inspection). This
predicts keys that existed at collect time; keys the engine invents during
replay are still served by the runtime exact-text fallback and reported in
`missing.jsonl`.

Success: `Successful: N, Fallback: 0`, `missing_llm=0 missing_fb=0
unparsed=0`, exit 0. `text_fallback=N` counts segments served from
`text_index.json` because their key did not exist at collect time; that is
normal and does not mean anything is missing.

`--log-file PATH` mirrors the console (including BabelDOC's own logs) into a
UTF-8 file; a relative path is resolved inside the work dir. On Windows this
avoids codepage loss and preserves a record of long runs.

Before a long run, `python <skill>/scripts/pipe_status.py --work <work>` reports
prompts/coverage, unanswered chunks, and estimates replay/collect time and peak
memory from the page count (coefficients measured on a 429-page book, ±25%).

If `missing_llm > 0`, inspect `missing.jsonl`: those keys were generated by
the engine itself and are not in `prompts.jsonl`, but their source text is.
Translate the text, not the key — re-run `pipe_merge.py` to refresh
`coverage_missing.jsonl`, translate it via `pipe_prep.py --from-missing`,
merge, and replay. (Stale miss logs are cleared at the start of each replay.)

### 4. Deliver + visual check

Outputs: `me_work/out_replay/<stem>.no_watermark.<lang>.mono.pdf` and
`.dual.pdf`. Copy to the user's requested destination. Spot-check with:

```bash
python <skill>/scripts/render_check.py <output.pdf> auto [--auto-count 6]
```

`auto` is text-aware: it skips pages with <200 characters (covers, blanks,
part-title separators), samples the remaining pages evenly across the book,
and adds up to two pages that start mid-sentence (cross-page paragraph merges —
where layout damage is most likely). Use explicit page numbers for a targeted
re-check. Then view the printed PNGs: translation present, no bleed-through
English, margins/page numbers intact, paragraphs spanning page/column breaks
coherent. Pages with no text layer (covers) stay English — tell the user.

## Failure quick-reference

See `references/contract.md` (prompt/key formats, engine validation) and
`references/troubleshooting.md` (error → fix table).

## Key invariants — never break these

- collect and replay: same PDF content + page range + `ME_LANG_IN` +
  `ME_LANG_OUT` (enforced via `run_stamp.json`; a mismatched replay exits 2
  before doing any work). Changing either language on collect archives the old
  run instead of silently reusing its translations.
- Never edit `prompts.jsonl` after collect — it is the key source.
- `<bN>` tags are preserved by the agent **byte-for-byte**; no re-injection
  script is used or needed.
- A translation may be reused across keys only when the **source text is
  byte-identical** (all `<bN>` tags included). Near-matches are reported as
  gaps, never guessed — tag renumbering is how formulas get corrupted.
- A green merge (exit 0) plus `missing_llm=0` means every source text in the
  document hit a translation; a green replay alone is not sufficient if merge
  reported gaps.
- `me_translator.py`'s `do_llm_translate(None)` probe must keep returning
  normally — any exception silently disables the whole LLM path.
- Green replay (`missing=0`, exit 0) means every prompt hit the mapping.

## Language pairs

Defaults `en→zh`. Set `ME_LANG_IN` / `ME_LANG_OUT` (e.g. `ja`, `en`) for
other directions; both must match between collect and replay (enforced), and
changing either on a re-collect archives the previous language's work instead
of reusing it. Output filenames use the target language suffix automatically.
