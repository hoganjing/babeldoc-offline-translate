# Troubleshooting (collect → build → replay)

| Symptom | Cause | Fix |
|---|---|---|
| `ScannedPDFError` / "Scanned PDF detected" at collect | PDF is a scan: page images contain the English, text layer is invisible OCR | set `ME_OCR_WORKAROUND=1` for collect AND replay. White-fill boxes cover the image's English, black text on top. No OCR engine needed. Pages with a completely empty text layer (covers) cannot be translated — expected (preflight lists them upfront). |
| `FzErrorSystem: code=2: cannot open file ...` at save time | Windows MAX_PATH: input stem (often a long book title) is mirrored into working/output paths > 260 chars | copy the PDF to a short name (`book.pdf`) and rerun both phases on that copy; deliver outputs under whatever final name the user wants. preflight warns about this before collect. |
| `UnicodeEncodeError` printing sources/warnings on Windows | cp936 console vs CJK output | `PYTHONUTF8=1` (PowerShell: `$env:PYTHONUTF8='1'`) before running any script; scripts also reconfigure stdout defensively |
| `bitstring` / `BitStream` ImportError from babeldoc | bitstring 5.x broke babeldoc's import | `pip install "bitstring<5"` |
| build exits 3: `tiktoken unavailable` | encoding files not downloadable (offline first run) | `pip install tiktoken`; first use downloads o200k_base (needs network once), then works offline. Never bypass — ratio validation would be silently skipped. |
| `prompts=0` after collect | wrong page range, image-only pages, or PDF path typo | verify page range; run `preflight.py` to see per-page text-layer stats |
| `unparsed > 0` | prompt matched neither batch nor single marker (babeldoc template changed) | read `me_work/unparsed.jsonl`; extend `INPUT_MARKER`/`SINGLE_MARKER` in me_translator.py, delete the file, re-collect |
| replay ERROR `collect/replay mismatch` (exit 2) | page range / PDF content sha / target language differ from `run_stamp.json` | rerun replay with the collected values, or re-collect for the new input (different PDF auto-archives the old run) |
| collect on a new PDF kept old stats/prompts | (fixed) — driver archives previous artifacts when the input fingerprint changes | verify `me_work/archive/` received the old files; rerun export after the new collect |
| want to extend a page range (1-5 → 1-15) | stamp records the collect range | just re-collect with the new range **on the same PDF** (append mode keeps old prompts); export syncs `zh_texts.json` keeping existing translations; translate only the `remaining` count, build, replay with the new range |
| build `RESULT: NEEDS_FIX` (coverage) | some values empty in zh_texts.json / slices | fill the listed sources (export prints `remaining` too) |
| build `RESULT: NEEDS_FIX` (validation) | same-as-input >10tok / ratio out of (0.3,3) / `<bN>` tag mismatch / paragraph count differs | each error names the source; fix that value, rerun build |
| build `unknown key(s) not present in src_texts` | zh object has keys from another run / hand-edited junk | remove the listed keys (or re-run export, which prunes them with a notice) |
| build `conflicting translations ... in zh_slice` | two slices translated the same source differently | pick one value; delete the other entry from its slice |
| glossary warnings (non-fatal) | a segment contains a glossary source term but not its target term | review each: fix genuine misses in the translation; delete the glossary entry if the term is intentionally rendered differently |
| replay `missing_llm>0` / WARNING / exit 1 | prompt key absent from llm_outputs.json (stale prompts, edited prompts) | inspect `missing.jsonl`; translate those sources, rebuild, replay (stale miss logs are cleared at replay start) |
| translated text invisible / double English | scanned image shows through where translation layer is thin | ensure `ME_OCR_WORKAROUND=1`; if page has 0 text chars it's image-only — cannot be fixed by this pipeline |
| untranslated page numbers / short headings / punctuation missing in scanned output | stock babeldoc z-order: untranslated pass-through chars paint before their own white mask | fixed automatically by `ocr_patch.py` (`ME_FIX_RECT_ORDER=1`, default). If missing, ensure you run via `run_translation.py` (not a hand-rolled babeldoc call) and the switch is not set to `0`; `ME_FIX_RECT_ORDER=0` reproduces stock behavior for A/B comparison |
| stray letter (e.g. `f`) printed on a scanned icon | OCR misreads an icon shape as a text char; the z-order fix (needed to reveal page numbers) also raises it above the mask that stock correctly hid | **accepted as harmless** — a drop-artifact pass was removed because telling an icon from a narrow multi-line text block is unreliable (risk of deleting real text). Do NOT "fix" by dropping chars in a tall-narrow mask |
| another OCR font renders nothing (glyph-less) | some scan-OCR fonts embed a CFF with 1 CharStrings (no glyphs) | `ocr_patch.py` substitutes a real 1-byte page font for ASCII codes of such chars. Set `ME_DEBUG_SUBST=1` to trace per-char substitution and glyph-count decisions |
| collect looked fine but replay output identical to input | capability probe failed (exception in `do_llm_translate(None)`) or wrong mode env | rerun replay; logs must show `Successful: N, Fallback: 0` |
| cover/title page still English | no text layer on that page (0 extractable chars) | expected; tell the user those pages need manual typesetting or real OCR re-creation |
| collect is slow (~1 min even for 5 pages) | babeldoc unconditionally typesets + renders output PDFs after translating (`high_level.py`); no supported switch to skip rendering while keeping the translation stage | accept the cost; collect runs once per input/range. Skipping would require a version-coupled monkeypatch of `PDFCreater.write` — deliberately not supported. |
| slices exist but build still reports missing | slices were generated before new sources were collected | rerun `split_work.py --force` (seeds existing translations), refill the new `""` entries |

## Sanity checklist before delivering

1. `preflight.py` report reviewed (paths, empty pages, OCR workaround?)
2. collect: `prompts=N  unparsed=0`
3. build: `RESULT: OK` (glossary warnings acceptable after review)
4. replay: `Successful: N  Fallback: 0`, `missing_llm=0 missing_fb=0`, exit 0
5. `render_check.py` PNGs: Chinese present, no bleed-through English,
   page numbers/margins intact, cross-page paragraphs coherent.
6. `ocr rect-order patch (ME_FIX_RECT_ORDER): ON` in the run log (scanned
   PDFs): page numbers and short untranslated headings must be visible.
