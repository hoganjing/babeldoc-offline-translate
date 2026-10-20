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
  `fallback_outputs.json`.
- Collect echoes input back; collect-run translation quality is irrelevant.

## Run parity (enforced by `run_stamp.json`)

Written after a successful collect:

```json
{"pages": "1-15", "sha256": "<pdf content hash>", "input": "<abs path>",
 "lang_out": "zh"}
```

- replay exits 2 **before any work** if pages / sha256 / lang_out differ.
- collect on a **different** sha256 archives prior artifacts
  (`prompts.jsonl`, `zh_texts.json`, `llm_outputs.json`, slices, stamp) to
  `me_work/archive/<hash8>_<ts>/` and starts clean.
- collect on the **same** sha256 appends to `prompts.jsonl` (incremental
  extension); `export_texts.py` then syncs `zh_texts.json` keeping existing
  translations, so only new sources need translating.
- Title-hint blocks vary between runs but never enter the keys.

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

## Pure-pipe flow (v2.5) — no source-keyed intermediate

`prompts.jsonl` is the single source of truth. The agent translates it
directly instead of going through `zh_texts.json` / `build_outputs.py`:

1. `pipe_prep.py` reads `prompts.jsonl` and splits it into `chunk_N.json`
   (round-robin, `--chunks N`). Each chunk carries an embedded instruction and
   a `records` list. A record is:
   - `batch`: `{"key":"b:...", "kind":"batch",
     "items":[{"id":N,"input":"<en with <bN>>","seed":null|"<zh>"}, ...]}`
   - `single`: `{"key":"s:...", "kind":"single", "source":"<en with <bN>>",
     "seed":null|"<zh>"}`
2. One subagent per chunk writes `llm_chunk_N.json` = `{"<key>": <value>}`:
   - batch value = JSON array `[{"id":N,"output":"<zh with <bN>>"}, ...]`
     (ids unchanged; this is what `do_llm_translate` returns on replay).
   - single value = plain zh string (this is what `do_llm_translate` returns
     for the single-path prompt).
3. `pipe_merge.py` globs `llm_chunk_*.json`, writes `llm_outputs.json`, and
   validates the `<bN>` tag sequence of every input against its output
   (lists `TAG-SEQUENCE PROBLEMS`). It also reuses single translations to seed
   `fallback_outputs.json` (covers the plain `do_translate` path if it fires).
4. `replay` serves the mapping exactly as in the v2.0 path.

Because the `<bN>` tags travel inside the text the agent sees, there is no
separate re-injection step that could misalign them — the only failure mode is
the agent dropping/duplicating a tag, which `pipe_merge.py` flags before
replay.

## Not supported (deliberate)

- Skipping collect-phase PDF rendering: `high_level.translate`
  unconditionally runs `Typesetting` + `PDFCreater.write` after translation;
  `skip_translation` also skips prompt capture (the whole point of
  collect). A monkeypatch of `PDFCreater.write` would work but is
  version-coupled and fragile — rejected; collect cost is accepted.
