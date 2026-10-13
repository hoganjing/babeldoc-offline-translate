---
name: babeldoc-offline-translate
description: Use when you need to translate a PDF book or document into another language while preserving the original typeset layout (bilingual dual PDF + mono PDF), but the environment has no reachable online machine-translation endpoint — babeldoc/BabelDOC's default backends (Google, DeepL, OpenAI, DeepSeek, SiliconFlow) fail or are blocked, so you must drive babeldoc with your own pre-translated dictionary instead of an online backend.
---

# BabelDOC Offline (Pre-Translated) PDF Translation

## Overview

**Core principle:** babeldoc does the parsing, layout detection, paragraph
splitting, font mapping and PDF typesetting. *You* supply the translations.
Instead of an online MT backend, you register a `PreTranslatedTranslator` that
only does dictionary lookup, and you perform the real translation **offline in
chunks** between two babeldoc runs.

This is the self-hosted alternative to the `translate-pdf` skill (which wraps
pdf2zh-next/BabelDOC with online backends). Use this when those backends are
unreachable — e.g. Google returns `ProxyError`, SiliconFlow `model_not_found`,
DeepL/OpenAI need keys you don't have.

## When to Use

- You must translate a PDF (book, paper, report) and **keep the original layout**.
- Online MT endpoints are blocked, rate-limited, or require keys you lack.
- You want **cross-paragraph / cross-page context** for quality — babeldoc
  hands you **one paragraph at a time**, so the only way to get context is to
  do the translation outside babeldoc, in larger chunks.
- You have an AI agent (or human) that can translate the extracted text.

**When NOT to use:** If an online backend works, prefer the `translate-pdf`
skill for a one-shot run. This skill is for the offline / self-hosted path.

## Architecture (3 phases)

```
PHASE 1 — RECORD (run babeldoc, do NOT translate)
  run_babeldoc.py --input book.pdf --record units.json --outdir record_work
      └─ PreTranslatedTranslator.do_translate() echoes原文 + 记录到 units.json
      └─ babeldoc 同时产出 record_work/<book>/translate_tracking.json (全书所有段)

PHASE 2 — OFFLINE TRANSLATE (babeldoc NOT running)
  extract_units.py  translate_tracking.json -> units_with_pages.json
  make_blocks.py    units_with_pages.json    -> blocks/block_NNN.json  (每 8 页一块)
  [你/Agent/子代理 逐块翻译]  blocks/block_NNN.json.units -> block_NNN.translations.json
  merge_translations.py  blocks/ -> translations.json   ({原文: 译文})

PHASE 3 — RENDER (run babeldoc again, now with the dictionary)
  run_babeldoc.py --input book.pdf --translations translations.json --outdir render_work
      └─ do_translate() 查表返回译文
      └─ babeldoc 原生排版 -> book.zh-Hans.mono.pdf + book.zh-Hans.dual.pdf
```

Key insight: babeldoc always sees **one paragraph**; the 8-page context is
assembled by YOU in Phase 2, entirely outside babeldoc. babeldoc source is
never modified — only its `BaseTranslator` extension point is used.

## Prerequisites

- **babeldoc installed** in the Python env that runs the scripts. Confirm with:
  `python -c "import babeldoc; print(babeldoc.__file__)"`.
  If missing: `pip install babeldoc`, or install the `translate-pdf` skill
  (it bundles `pdf2zh-next`, which depends on `babeldoc`).
- A target PDF to translate.

## Files in this skill

```
babeldoc-offline-translate/
├── SKILL.md
├── scripts/
│   ├── run_babeldoc.py        # Phase 1/3 driver (record / render)
│   ├── extract_units.py       # Phase 2: tracking.json -> units_with_pages.json
│   ├── make_blocks.py         # Phase 2: split units into N-page blocks
│   ├── merge_translations.py  # Phase 2: merge block_*.translations.json -> one dict
│   └── doclayout_demo.py      # diagnostic: see what babeldoc's model "sees"
└── references/
    └── translator_prompt_template.md  # prompt for translation subagents
```

Run scripts with the **managed python venv** where babeldoc lives, e.g.:
`C:\Users\j\.workbuddy\binaries\python\envs\default\Scripts\python.exe scripts/run_babeldoc.py ...`

## Workflow (step by step)

### Phase 1 — Record
```bash
python scripts/run_babeldoc.py \
  --input book.pdf --record units.json --outdir record_work
```
This emits `record_work/<book>/translate_tracking.json` (the full source text,
segmented by babeldoc) plus `units.json` (the recorded original units).

### Phase 2 — Offline translate
```bash
python scripts/extract_units.py --tracking record_work/<book>/translate_tracking.json --out units_with_pages.json
python scripts/make_blocks.py   --units units_with_pages.json --out-dir blocks --block-pages 8
# Translate each blocks/block_NNN.json ("units" array) -> blocks/block_NNN.translations.json
# Use references/translator_prompt_template.md as the subagent prompt.
python scripts/merge_translations.py --blocks-dir blocks --out translations.json
```
The dictionary key MUST equal the original unit text **character-for-character**
(包括空格/标点/占位符 `<b1>` `</b1>` `{v1}`). Any mismatch → that unit stays
untranslated and shows up in `render_work/missing.json`.

### Phase 3 — Render
```bash
python scripts/run_babeldoc.py \
  --input book.pdf --translations translations.json --outdir render_work
```
Outputs `*.zh-Hans.mono.pdf` (translated only) and `*.zh-Hans.dual.pdf`
(bilingual). If `render_work/missing.json` is non-empty, some units were not in
the dictionary — fix the key mismatch and re-run Phase 3 (no need to redo Phase 1).

## Common Mistakes

- **Editing babeldoc source.** Never. Register `PreTranslatedTranslator` only.
- **Dict key not exact.** `do_translate` matches by exact string. One stray
  space, newline, or missing placeholder tag → miss. Always validate keys.
- **Forgetting `--translations` ⇒ record mode.** Without it, `run_babeldoc.py`
  records instead of renders. Check which phase you are in.
- **Assuming babeldoc merges cross-page paragraphs.** In the standard (non-LLM)
  path it does NOT — each half is a separate unit. The merge-equivalent work is
  done by YOU in Phase 2 (whole block in context). Keep each unit intact;
  do not pre-concatenate units before translating (babeldoc needs each unit
  separately to place it back).
- **`do_llm_translate` must raise `NotImplementedError`.** Otherwise babeldoc
  takes the LLM-prompt branch and your dictionary is ignored.
- **Translating `cross_page`/`cross_column` units.** `extract_units.py` includes
  them (page = -1). Translate them too, or they will be missing.

## Real-World Impact

Used to translate a 158-page edited volume (Explorers of Humankind) end-to-end:
record → 20 blocks × 8 pages → agent-translated → merged → rendered dual+mono
PDF, with layout, figures, and page headers preserved and untouched.
