---
name: babeldoc-offline-translate
description: >-
  Offline collect→translate→replay pipeline that makes the agent itself the
  LLM translation backend for BabelDOC / pdf2zh PDF translation (layout
  preserved, EN→ZH by default, any language pair via env). Use when
  translating a PDF with babeldoc or pdf2zh without a live OpenAI/Anthropic
  API key, when the user says "翻译 PDF", "translate this PDF with babeldoc",
  "pdf2zh offline", "做离线翻译", "collect/replay 翻译", or when a BabelDOC
  run must serve pre-translated text through a custom BaseTranslator.
---

# BabelDOC Offline Translator (agent-as-LLM)

Turn the agent into BabelDOC's translation API. No network calls, no API
keys: a **collect** run dumps every prompt, the agent translates offline, a
replay run serves the pre-translated text and writes the final PDFs.

## When to use

- Translate a PDF (layout preserved) with babeldoc/pdf2zh but there is no
  usable LLM API key, or the user explicitly wants the agent to translate.
- Resume/extend a translation where prompts were already collected.

Do NOT use for: one-off text translation with no PDF involved; live-API
babeldoc runs (`babeldoc` CLI with `--openai` etc. works fine as-is).

## Prerequisites

- Python with `babeldoc` 0.6.x installed: `pip install babeldoc "bitstring<5"`
  (`bitstring>=5` breaks babeldoc — pin it). `tiktoken` required (build fails
  loudly without it — validation must never silently skip).
- On Windows set `PYTHONUTF8=1` in the session before running anything
  (prints include CJK source text; cp936 consoles crash otherwise).
- Scripts live in `<skill>/scripts/`: `me_translator.py`,
  `run_translation.py`, `export_texts.py`, `build_outputs.py`,
  `preflight.py`, `render_check.py`, `split_work.py`.

## File contract (work dir `./me_work`, override with `ME_WORK_DIR`)

| File | Shape | Written by |
|---|---|---|
| `prompts.jsonl` | append-only prompt records | collect |
| `run_stamp.json` | `{pages, sha256, input, lang_out}` | collect |
| `src_texts.json` | JSON **array** of unique sources | export |
| `zh_texts.json` | JSON **object** `{"source": "translation"}` | export creates/merges, agent fills |
| `zh_slice_NN.json` | same object, parallel slices | split_work / agent fills |
| `glossary.json` | object `{"source term": "target term"}` (optional) | agent |
| `llm_outputs.json`, `fallback_outputs.json` | keyed maps | build |

Translations are **source-keyed, never index-aligned** — misalignment is
structurally impossible. Empty string value = not yet translated.

## Workflow (strict order)

Run everything from ONE working directory. `collect` and `replay` must use
the same PDF content, page range, and target language (enforced by
`run_stamp.json`).

### 0. Preflight

```bash
export PYTHONUTF8=1   # Windows PowerShell: $env:PYTHONUTF8='1'
python <skill>/scripts/preflight.py <input.pdf>
```

Read the report first: zero-text pages (won't translate), image-dominant
pages (`ME_OCR_WORKAROUND=1` needed if collect raises ScannedPDFError), long
path warning (copy PDF to a short ASCII name — Windows MAX_PATH), babeldoc
version warning. Also copy the PDF to a short name proactively when warned.

### 1. Collect

```bash
python <skill>/scripts/run_translation.py collect <input.pdf> [page_range]
```

Success: `[me] prompts=N ... unparsed=0`.
- `prompts=0` → wrong range / image-only PDF: investigate before proceeding.
- `ScannedPDFError` → rerun with `ME_OCR_WORKAROUND=1` (collect AND replay).
- Re-collect on a **different** PDF automatically archives the previous
  run's artifacts to `me_work/archive/<hash>_<ts>/` and starts clean.
- Re-collect on the **same** PDF (e.g. extended page range) appends —
  earlier prompts and translations are kept (incremental).

### 2. Export sources

```bash
python <skill>/scripts/export_texts.py
```

Writes `src_texts.json` (array) and syncs `zh_texts.json` (object: adds `""`
placeholders for new sources, keeps existing translations, prunes stale keys,
prints coverage `X/Y translated`).

### 3. Translate offline (this is where the agent IS the LLM)

Fill every value in `zh_texts.json`. Small batches: edit the object directly.
Large batches: `python <skill>/scripts/split_work.py [char_budget]` →
`zh_slice_01.json`... hand one slice per parallel subagent (each returns the
same object filled), then `build_outputs.py` auto-merges all slices.

Translation rules (build validates 1–5; the engine re-validates on replay):

1. Output ≠ input when input > 10 tokens (never echo the source).
2. Token ratio output/input within (0.3, 3) (tiktoken gpt-4o). Short
   catalog codes/titles: translate, don't drop; don't over-compress long
   paragraphs; gloss awkward short titles in parentheses to satisfy the
   ratio rule.
3. `<bN>` tags byte-identical, same sequence, same relative position.
4. Paragraph count equal (`\n` splits — keep translations single-line).
5. No key outside the source list (export owns the key set; slices must
   cover sources exactly).
6. Terminology consistency: create `me_work/glossary.json`
   (`{"Spinal Engine": "脊柱引擎", ...}`) up front; build reports segments
   missing a glossary term (non-fatal warnings).
7. Keep person/publisher names and ISBN/catalog codes in Latin script;
   translate the meaning while silently correcting OCR garble
   (`J OO%` → `100%`).

### 4. Build + validate

```bash
python <skill>/scripts/build_outputs.py
```

Must print `RESULT: OK`. FATAL (`NEEDS_FIX`): untranslated sources
(coverage), same-as-input, ratio, tag/paragraph mismatch, unknown keys,
conflicting slice values, **tiktoken unavailable** (fix: `pip install
tiktoken`; first use needs network once). Glossary misses print as
non-fatal warnings.

### 5. Replay

```bash
python <skill>/scripts/run_translation.py replay <input.pdf> [page_range]
```

Refuses on any collect/replay mismatch (pages, PDF content sha, target
language) — fix by re-running collect or replay with the collected values.
Success: `Successful: N, Fallback: 0`, `missing_llm=0 missing_fb=0
unparsed=0`, exit 0. Missing > 0 → inspect `missing.jsonl`, translate the
new sources, rebuild, replay again (stale miss logs are cleared at start).

### 6. Deliver + visual check

Outputs: `me_work/out_replay/<stem>.no_watermark.<lang>.mono.pdf` and
`.dual.pdf`. Copy to the user's requested destination. Spot-check with:

```bash
python <skill>/scripts/render_check.py <output.pdf> auto
```

then view the printed PNGs: translation present, no bleed-through English,
margins/page numbers intact, paragraphs spanning page/column breaks coherent.
Pages with no text layer (covers) stay English — tell the user.

## Failure quick-reference

See `references/contract.md` (prompt/key formats, engine validation,
cross-page/column batching) and `references/troubleshooting.md`
(error → fix table).

## Key invariants — never break these

- collect and replay: same PDF content + page range + `ME_LANG_OUT`
  (enforced via `run_stamp.json`; a mismatched replay exits 2 before doing
  any work).
- Never edit `prompts.jsonl` after collect — it is the key source.
- `zh_texts.json` is a source-keyed object; never convert it to an array,
  never dedupe/reorder keys.
- `me_translator.py`'s `do_llm_translate(None)` probe must keep returning
  normally — any exception silently disables the whole LLM path.
- Green replay (`missing=0`, exit 0) means every prompt hit the mapping.

## Language pairs

Defaults `en→zh`. Set `ME_LANG_IN` / `ME_LANG_OUT` (e.g. `ja`, `en`) for
other directions; `ME_LANG_OUT` must match between collect and replay
(enforced). Output filenames use the target language suffix automatically.
