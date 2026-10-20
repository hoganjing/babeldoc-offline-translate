---
name: babeldoc-offline-translate
description: >-
  Offline collect→translate→replay pipeline that makes the agent itself the
  LLM translation backend for BabelDOC / pdf2zh PDF translation (layout
  preserved, EN→ZH by default, any language pair via env). v2.5 ("pure-pipe"):
  feed BabelDOC's verbatim prompts straight to the agent, preserve <bN> tags
  byte-for-byte, merge responses directly into llm_outputs.json — no
  source-keyed intermediate and no tag re-injection step. Use when
  translating a PDF with babeldoc or pdf2zh without a live OpenAI/Anthropic
  API key, when the user says "翻译 PDF", "translate this PDF with babeldoc",
  "pdf2zh offline", "做离线翻译", "collect/replay 翻译", or when a BabelDOC
  run must serve pre-translated text through a custom BaseTranslator.
---

# BabelDOC Offline Translator (agent-as-LLM) — v2.5 pure-pipe

Turn the agent into BabelDOC's translation API. No network calls, no API
keys: a **collect** run dumps every prompt, the agent translates offline, a
**replay** run serves the pre-translated text and writes the final PDFs.

## v2.5 in one paragraph

BabelDOC's own LLM path sends one prompt per batch (a JSON array of
paragraphs) plus a single-paragraph fallback. v2.5 captures those prompts
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
- On Windows set `PYTHONUTF8=1` in the session before running anything
  (prints include CJK source text; cp936 consoles crash otherwise).
- Scripts live in `<skill>/scripts/`: `me_translator.py`, `run_translation.py`
  (collect/replay core), `pipe_prep.py`, `pipe_merge.py` (pure-pipe), plus the
  retained v2.0 helpers `export_texts.py`, `build_outputs.py`,
  `preflight.py`, `render_check.py`, `split_work.py`.

## File contract (work dir `./me_work`, override with `ME_WORK_DIR`)

| File | Shape | Written by |
|---|---|---|
| `prompts.jsonl` | one record per prompt: `{"kind","key","prompt"\|"source"}` | collect |
| `run_stamp.json` | `{pages, sha256, input, lang_out}` | collect |
| `chunk_N.json` | `{"chunk","instruction","records":[...]}` for subagents | `pipe_prep.py` |
| `llm_chunk_N.json` | `{"<key>": <value>}` — one per subagent | subagent |
| `llm_outputs.json` | keyed `b:`/`s:` map (batch value = JSON string) | `pipe_merge.py` |
| `fallback_outputs.json` | keyed by sha(source) | `pipe_merge.py` (reused) |
| `src_texts.json` / `zh_texts.json` / `zh_slice_*.json` | v2.0 source-keyed path only | v2.0 helpers |

Translations are keyed by BabelDOC's own cache keys, never by list index —
misalignment is structurally impossible.

## Workflow (strict order)

Run everything from ONE working directory. `collect` and `replay` must use the
same PDF content, page range, and target language (enforced by
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
- Re-collect on a **different** PDF automatically archives the previous run's
  artifacts to `me_work/archive/<hash>_<ts>/` and starts clean.
- Re-collect on the **same** PDF (e.g. extended page range) appends —
  earlier prompts are kept (incremental).

### 2. Translate offline — PURE-PIPE (recommended, v2.5)

```bash
# 2a. split prompts into N chunks for parallel subagents
python <skill>/scripts/pipe_prep.py --chunks 8 [--seed me_work/zh_texts.json]
# 2b. hand each me_work/chunk_N.json to one parallel subagent.
#     Each subagent follows the embedded instruction and writes
#     me_work/llm_chunk_N.json = {"<key>": <value>}.
# 2c. merge all replies + validate <bN> tag integrity
python <skill>/scripts/pipe_merge.py
```

**Subagent contract** (also embedded in every chunk file):
- `batch` record: translate each `items[].input` to Chinese, keep every
  `<bN>` tag byte-for-byte (same sequence, same relative position), return
  `value` = `[{"id": N, "output": "<zh with <bN>>"}, ...]` — a JSON array
  with ids unchanged.
- `single` record: translate `source` to Chinese, keep `<bN>` tags, return
  `value` = the plain zh string.
- Never invent keys — each record already carries its exact key.
- `--seed` (optional, re-runs only): attaches a prior `zh_texts.json` entry as
  a quality reference; the agent must still re-place tags per the rules.

`pipe_merge.py` prints `merged keys`, `TAG-SEQUENCE PROBLEMS: 0`, and writes
`llm_outputs.json` (it also reuses single translations to seed
`fallback_outputs.json` for the plain do_translate path). If
`TAG-SEQUENCE PROBLEMS > 0`, fix those records' translations and re-merge —
do **not** hand-edit tags.

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
python <skill>/scripts/run_translation.py replay <input.pdf> [page_range]
```

Refuses on any collect/replay mismatch (pages, PDF content sha, target
language) — fix by re-running collect or replay with the collected values.
Success: `Successful: N, Fallback: 0`, `missing_llm=0 missing_fb=0
unparsed=0`, exit 0. Missing > 0 → inspect `missing.jsonl`, translate the
new sources, re-merge (`pipe_merge.py`) or rebuild, replay again (stale miss
logs are cleared at start).

### 4. Deliver + visual check

Outputs: `me_work/out_replay/<stem>.no_watermark.<lang>.mono.pdf` and
`.dual.pdf`. Copy to the user's requested destination. Spot-check with:

```bash
python <skill>/scripts/render_check.py <output.pdf> auto
```

then view the printed PNGs: translation present, no bleed-through English,
margins/page numbers intact, paragraphs spanning page/column breaks coherent.
Pages with no text layer (covers) stay English — tell the user.

## Failure quick-reference

See `references/contract.md` (prompt/key formats, engine validation) and
`references/troubleshooting.md` (error → fix table).

## Key invariants — never break these

- collect and replay: same PDF content + page range + `ME_LANG_OUT`
  (enforced via `run_stamp.json`; a mismatched replay exits 2 before doing
  any work).
- Never edit `prompts.jsonl` after collect — it is the key source.
- `<bN>` tags are preserved by the agent **byte-for-byte**; no re-injection
  script is used or needed.
- `me_translator.py`'s `do_llm_translate(None)` probe must keep returning
  normally — any exception silently disables the whole LLM path.
- Green replay (`missing=0`, exit 0) means every prompt hit the mapping.

## Language pairs

Defaults `en→zh`. Set `ME_LANG_IN` / `ME_LANG_OUT` (e.g. `ja`, `en`) for
other directions; `ME_LANG_OUT` must match between collect and replay
(enforced). Output filenames use the target language suffix automatically.
