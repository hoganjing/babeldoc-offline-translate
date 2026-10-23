# BabelDOC translator contract (what the offline translator must satisfy)

Installed version verified against: babeldoc 0.6.4 (site-packages).
`preflight.py` warns when a different version is installed.

## Where the translator plugs in

- Subclass `babeldoc.translator.translator.BaseTranslator`
  (`translator/translator.py`). Implement:
  - `do_llm_translate(text, rate_limit_params) -> str` — the LLM path
    (`ILTranslatorLLMOnly`): cross-page/cross-column merging, batch
    grouping, contextual title hints, glossary.
  - `do_translate(text, rate_limit_params) -> str` — the plain per-paragraph
    path (no context, no batching).
- `do_llm_translate(None)` is a **capability probe**
  (`translator_supports_llm`, `high_level.py`): must return normally. Any
  exception ⇒ engine silently uses the plain HTTP path and all your prompts
  are bypassed.
- `translator.name` must be ≤ 20 chars (cache schema limit).
- Engine entry: `high_level.translate(cfg)` with
  `TranslationConfig(translator=..., pages=..., lang_in/out, output_dir=...,
  working_dir=..., ocr_workaround=...)`.

## The two prompt formats

### 1. Batch (LLM path — `il_translator_llm_only.py`)

- Marker: `## Here is the input:` followed by a JSON array:
  `[{"id": 0, "input": "...", "layout_label": "text"}, ...]`
- Response: JSON array of `{"id": <same id>, "output": "<translation>"}`
  covering every input id.
- Batch composition (engine-side, before calling you):
  - `process_page`: consecutive paragraphs accumulate until token > 200 or
    count > 5.
  - `process_cross_page_paragraph`: last body paragraph of page N + first
    body paragraph of page N+1 always share one request (separate ids,
    shared context; results split back per paragraph — boundaries never
    merge).
  - `process_cross_column_paragraph`: same-page adjacent body paragraphs
    with y-gap > 20 units.
- Contextual hints (first paragraph, recent title) wrap the JSON payload and
  may differ run-to-run — which is why lookup keys hash the **JSON payload
  only**, not the whole prompt.

### 2. Single / fallback (plain path — `il_translator.generate_prompt_for_llm`)

- Marker: `Now translate the following text:` + raw source. Response must
  be the **plain translated text** (not JSON).

## Engine-side validation (batch path; failure ⇒ per-paragraph fallback)

- output ≠ input when input > 10 tokens (gpt-4o tiktoken).
- token ratio output/input ∈ (0.3, 3).
- same paragraph count (split on `\n`).
- `<bN>` style tags byte-identical, same sequence.
`build_outputs.py` enforces all of these (plus full coverage) BEFORE replay,
with a hard failure when tiktoken itself is unavailable — validation must
never silently degrade.

## Key scheme (me_translator.py)

- batch:  `"b:" + sha256(canonical_json_payload)` — payload re-serialized
  `ensure_ascii=False, indent=2` after parsing.
- single: `"s:" + sha256(source_text)`.
- plain `do_translate`: `sha256(source_text)` (no prefix) →
  `fallback_outputs.json`. `pipe_merge.py` backfills this file from the **whole**
  text index (not only `fallback_src.jsonl`), and `do_translate` additionally
  consults `text_index.json` on a miss, so a replay-time fallback still returns
  a translation instead of the source text.
- Collect echoes input back; collect-run translation quality is irrelevant.

## Run parity (enforced by `run_stamp.json`)

Written after a successful collect:

```json
{"pages": "1-15", "sha256": "<pdf content hash>", "input": "<abs path>",
 "lang_in": "en", "lang_out": "zh"}
```

- replay exits 2 **before any work** if pages / sha256 / lang_in / lang_out
  differ, or if a stamp written before this field existed is paired with a
  different current `ME_LANG_IN`.
- collect archives all prior artifacts (prompts, outputs, `text_index` /
  `fallback_outputs`, chunks, chunk retry/fix metadata, retry/coverage reports,
  and `out_collect`/`out_replay` directories) to
  `me_work/archive/<hash8>_<ts>/` and starts clean when the PDF content
  changes, either language changes, or the page range changes in a way that is
  not a strict extension (including an unparseable range expression).
- collect on the **same** sha256 and languages with an unchanged or strictly
  extended page range appends to `prompts.jsonl` (incremental extension) and
  never writes a key already present in `prompts.jsonl`/`fallback_src.jsonl`;
  `export_texts.py` then syncs `zh_texts.json` keeping existing translations,
  so only new sources need translating.
- Title-hint blocks vary between runs but never enter the keys.
- Windows: when the input path is within 120 characters of MAX_PATH, the driver
  copies the PDF to `%TEMP%\me_short\<sha8>\<stem>.pdf`, runs with short
  input/output/working dirs, then copies the rendered PDFs back to
  `out_<mode>/`. The stamp still records the original path and content sha, so
  collect/replay parity is unchanged.

## Data shapes in the work dir

- `src_texts.json`: array of unique sources (first-appearance order).
- `zh_texts.json` / `zh_slice_NN.json`: object `{"source": "translation"}` —
  source-keyed, so index misalignment is impossible. `""` = untranslated.
  build auto-merges slices; conflicting non-empty values for one source are
  fatal; keys outside `src_texts.json` are fatal.
- `glossary.json`: object `{"source term": "target term"}` — build warns
  (non-fatal) when a segment contains the source term but not the target.

## Output files

- `<output_dir>/<stem>.no_watermark.<lang_out>.mono.pdf` — target only.
- `<output_dir>/<stem>.no_watermark.<lang_out>.dual.pdf` — bilingual.
- `working_dir/translate_tracking.json` (written when working_dir set):
  `cross_page` / `cross_column` / `page` groups — audit how many segments
  were merged across page/column breaks.

## Pure-pipe flow (v2.6) — no source-keyed intermediate

`prompts.jsonl` is the single source of truth. The agent translates it
directly instead of going through `zh_texts.json` / `build_outputs.py`:

1. `pipe_prep.py` reads `prompts.jsonl` and splits it into `chunk_N.json`.
   Chunks are packed by **source characters** (`--chunks` = target count,
   `--max-chars` = hard cap, default 32000) instead of round-robin by record,
   because batch records differ in size by an order of magnitude and oversized
   chunks time out. Prep is incremental by default:
   records whose exact source already has a non-empty translation in
   `text_index.json` are skipped (a batch only when every item is covered);
   `--all` re-chunks everything. Each chunk carries `instruction`, `output` (the
   exact file the subagent must write), `keys`, and `records`. A record is:
   - `batch`: `{"key":"b:...", "kind":"batch",
     "items":[{"id":N,"input":"<en with <bN>>","seed":null|"<zh>"}, ...]}`
   - `single`: `{"key":"s:...", "kind":"single", "source":"<en with <bN>>",
     "seed":null|"<zh>"}`
   - `text`: `{"key":"t:sha256(source)", "kind":"text", "source":"..."}` —
     only from `--from-missing` / `--from-retry`, for coverage/quality fix-ups.
2. One subagent per chunk writes the file named by the chunk's `output` field
   (`llm_chunk_N.json` / `llm_chunk_fix_N.json`) = `{"<key>": <value>}`:
   - batch value = JSON array `[{"id":N,"output":"<zh with <bN>>"}, ...]`
     (ids unchanged; this is what `do_llm_translate` returns on replay).
   - single/text value = plain zh string.
3. `pipe_merge.py` globs `llm_chunk_*.json` (including `llm_chunk_retry_*.json`),
   writes `llm_outputs.json`, and validates every record (tag sequence, batch id
   completeness, empty output, duplicate-key conflicts). It also builds
   `text_index.json` and uses it to fill keys no agent wrote; see "Coverage
   model" below.
4. `replay` serves the mapping exactly as in the v2.0 path.

## Coverage model (why keys are not the unit of work)

BabelDOC's keys are engine-internal and **not stable across runs**:

- `b:<sha256(canonical batch payload)>` changes when batch composition changes
  (same paragraphs, different grouping).
- `s:<sha256(source text)>` appears for paragraphs that were handled by the
  batch path during collect.

Measured on a 429-page book: 2852 prompt keys, but only 2504 unique source
texts; 1710 keys were absent from the agent's output yet harmless because the
identical text had been translated elsewhere. Treating keys as the unit
produced a meaningless `MISSING KEYS: 1757`, hid 84 genuinely untranslated
segments, and cost a full replay to discover them.

`pipe_merge.py` therefore builds `text_index.json` =
`{sha256(source): translation}` from every translated record (batch items and
singles) and fills any key whose **source text matches byte-for-byte**,
including every `<bN>` tag. No tags are ever re-mapped: a near-match is a gap,
not a guess.

Outputs:

- `coverage_report.json` — key/text coverage, gap count, conflicts, problems.
- `coverage_missing.jsonl` — text-addressed gaps, same shape replay's
  `missing.jsonl` would report: `{"key":"t:<sha>","kind":"text","source":...}`.
  Feed it to `pipe_prep.py --from-missing coverage_missing.jsonl`, translate,
  merge again; the gap texts enter the index and every dependent key is
  back-filled.
- Exit codes: `0` complete, `2` missing inputs, `3` incomplete (gaps, tag
  problems, retry values with invalid tags, structural problems, or unresolved
  conflicts). `--allow-gaps` downgrades gaps only;
  `--conflict {error,first,last}` resolves duplicate keys.
- `content_warnings.jsonl` — content-level suspects that tag/id checks cannot
  see: output identical to source with >10 words, no target-script characters in
  a segment ≥200 chars, target under 10% / over 4× source length. Only prose-like
  sources are judged (≥6 lowercase words after stripping `<bN>` tags), and
  bibliography lines (`et al`, years, DOI, ISBN, `pp.`) are exempt, so formulas,
  names and address lists do not produce noise. All checks are advisory unless
  `--strict`.
- `retry.jsonl` — every segment still defective after merge, text-addressed:
  `{"key":"t:<sha>","kind":"text","source":...,"reasons":[...]}`. Reasons include
  `tag mismatch`, `empty translation`, `missing id in output`, `unusable batch
  output`, `missing translation`, and (opt-in) `content: ...` / replay misses.
  The file is deleted when empty.
- `glossary_warnings.jsonl` — a glossary source term occurs in a segment but its
  target term is absent from the output. Advisory; `--no-glossary` disables it.

## Retry loop (text-addressed)

`pipe_merge.py` detects defects but never edits a chunk file. Instead it writes
`retry.jsonl`; `pipe_prep.py --from-retry retry.jsonl` emits
`chunk_retry_N.json` → `llm_chunk_retry_N.json`. On the next merge those values
(`t:<sha>`) are applied **with precedence**, unless a normal chunk covering the
same source is newer: they override the text index and rebuild any dependent
batch/single output, so a single broken item can be fixed without retranslating
its whole batch or chunk. A value whose tag sequence does not match the source
is never applied: it is listed under `RETRY VALUES WITH BAD TAGS`, stays in the
retry list, and makes merge exit 3 even when the normal value is fine.

Two flags widen the list: `--retry-content` adds advisory content warnings
(normally left to `content_warnings.jsonl`), and `--import-missing` folds the
replay-time `missing.jsonl` / `missing_fallback.jsonl` (any source already
covered by the current index is skipped). Imported misses count as gaps for the
exit code; they do not silently pass.

Recency is per source text, not per file: for every `t:<sha>` value, merge
compares the fix file's `mtime_ns` with the newest normal `llm_chunk_*.json`
that covers the same source. An older fix is ignored (counted as
`retry_ignored` in `coverage_report.json`, with a console notice), because a
newer normal chunk is a re-translation whose output should win. Re-save or
regenerate `llm_chunk_retry_*.json` after the normal chunk when the retry value
must win. This prevents a retry file from a finished round overriding a later,
better translation.

At replay time `me_translator.py` also consults `text_index.json` **after a key
miss** (batch: all ids must be covered; single: exact source) and serves that
translation. This catches keys that did not exist at collect time (observed:
13 such `s:` keys on the same PDF). `ME_TEXT_FALLBACK=0` disables it. It only
ever returns the translation of an identical source string, so BabelDOC's
segmentation, batching, tag numbering and layout are unchanged.

The plain `do_translate` path is covered twice: merge writes every indexed
translation into `fallback_outputs.json` (previously only sources seen in
`fallback_src.jsonl`, which was empty when collect never took that path), and
`do_translate` falls back to the same exact-text lookup when the sha is absent
from that file. A replay-time fallback therefore emits the target text even if
the engine invents a new plain-path request.

## Replay preflight (before rendering)

`run_translation.py replay` calls `replay_precheck.py` after the
`run_stamp.json` parity check and **before loading the layout model**. It
recomputes coverage from `prompts.jsonl`, `llm_outputs.json` and
`text_index.json` using the same rules as the runtime lookup:

- a prompt is covered by key when its value is present and usable (batch: every
  id, non-empty outputs);
- otherwise it is covered by text when every source string it contains hashes
  to an entry in `text_index.json`;
- otherwise it is a gap.

It also checks the plain `do_translate` path: every `fallback_src.jsonl` entry
must resolve in `fallback_outputs.json` or `text_index.json` (runtime lookup),
otherwise it is reported as a `kind: "fallback"` gap. This is the set of
paragraphs the engine already tried to route around the batch/single keys.

Any gap aborts with exit 3 and the merge/`--from-missing` commands to fix it;
`--allow-misses` overrides and `--precheck-only` reports without rendering.
Keys the engine invents during replay cannot be predicted; the runtime
exact-text fallback and `missing.jsonl` still apply to them, so the preflight is
a lower bound, not a guarantee.

Preflight cost is ~0.2 s for 2852 prompts (it never touches the PDF). JSON
mapping files are read as `utf-8-sig`, so a BOM from Windows editors is
tolerated; a file that cannot be parsed is a hard error (exit 2) rather than
being silently treated as empty.

Because the `<bN>` tags travel inside the text the agent sees, there is no
separate re-injection step that could misalign them — the only failure mode is
the agent dropping/duplicating a tag, which `pipe_merge.py` flags before
replay.

## Glossary injection (agent-side only)

`glossary.json` = `{"source term": "target term"}`. When `pipe_prep.py` finds
one (auto: `<work>/glossary.json`; or `--glossary PATH`), it adds a numbered
rule to each chunk's `instruction`:

- only terms that actually occur in that chunk's sources are included;
- matching is word-boundary and case-insensitive for ASCII terms, so `motor`
  does not match `motorcycle`;
- at most 60 terms per chunk, ordered by frequency then length (longer phrases
  first);
- `--no-glossary` disables it.

This is deliberately confined to the agent instructions: `prompts.jsonl`, the
`b:`/`s:` keys and everything BabelDOC sends or receives are untouched, so
turning a glossary on cannot change segmentation, batching, tag numbering or
layout. `pipe_merge.py` reports adherence after translation: for every segment
containing a glossary source term it checks that the target term appears in the
output, and lists misses in `GLOSSARY WARNINGS` / `glossary_warnings.jsonl`.
This is advisory, not a hard failure, because translation is not deterministic;
`--no-glossary` skips the check.

## Self-test

`scripts/selftest.py` builds a synthetic work dir under `%TEMP%` and exercises
the whole pure-pipe loop without a PDF or BabelDOC: merge → tag defect →
`retry.jsonl` → `pipe_prep --from-retry` → merge complete, bad retry tags
gating (exit 3 + `retry_bad_tags`), stale-retry mtime handling
(`retry_ignored`), content/glossary warnings, incremental prep, `--all`
re-chunking, preflight, collect archiving (including chunk metadata and
language identity), prompt-key dedupe, first-marker prompt parsing, runtime text
fallback, and the Windows short-path helpers. `--work DIR` additionally audits a
real work dir read-only (artifact parse, preflight coverage, retry absent).

```bash
python scripts/selftest.py [--work me_work]
```

## Not supported (deliberate)

- Skipping collect-phase PDF rendering: `high_level.translate`
  unconditionally runs `Typesetting` + `PDFCreater.write` after translation;
  `skip_translation` also skips prompt capture (the whole point of
  collect). A monkeypatch of `PDFCreater.write` would work but is
  version-coupled and fragile — rejected; collect cost is accepted.
