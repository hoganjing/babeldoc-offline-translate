"""Two-phase BabelDOC driver using MeTranslator (offline agent translation).

Usage:
  python run_translation.py collect <input.pdf> [page_range e.g. 1-5]
  python run_translation.py replay  <input.pdf> [page_range]

IMPORTANT: collect and replay MUST use the same input file (same content)
and the same page range, otherwise the batch keys will not match. The driver
enforces this via <work>/run_stamp.json which records page range, a sha256
fingerprint of the PDF, and the output language.

Phase 1 (collect) runs the full BabelDOC pipeline with placeholder echo
translations and dumps every LLM batch prompt to <work>/prompts.jsonl.
The agent then translates those batches offline into <work>/llm_outputs.json.
Phase 2 (replay) re-runs the pipeline serving the pre-translated text,
producing the final mono + dual PDFs under <work>/out_replay/.

Work dir: $ME_WORK_DIR or ./me_work under the current working directory.

Environment switches:
   ME_OCR_WORKAROUND=1  scanned PDF (text baked into page images with an
                        invisible OCR layer): white-fill boxes mask the
                        English in the image, black text is written on top.
                        Also implied by ScannedPDFError on collect.
   ME_FIX_RECT_ORDER=1  (default) keep white OCR masks UNDER untranslated
                        pass-through text so page numbers / short headings
                        stay visible (runtime monkeypatch, O in-process,
                        site-packages untouched). =0 disables (stock
                        babeldoc behavior: masks can cover pass-through text).
                        See scripts/ocr_patch.py for the root cause.
   ME_LANG_IN / ME_LANG_OUT   source/target language (default en / zh).
                        Must match between collect and replay (enforced).
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
import time
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

WORK = Path(
    os.environ.get("ME_WORK_DIR") or (Path.cwd() / "me_work")
).resolve()
STAMP = "run_stamp.json"

# Files owned by a collect run; archived together when the input changes.
_COLLECT_ARTIFACTS = (
    "prompts.jsonl",
    "src_texts.json",
    "zh_texts.json",
    "llm_outputs.json",
    "fallback_outputs.json",
    "fallback_src.jsonl",
    STAMP,
)


def _count_lines(path: Path) -> int:
    if not path.exists():
        return 0
    with path.open(encoding="utf-8") as f:
        return sum(1 for _ in f)


def _fingerprint(pdf: str, pages: str | None, lang_out: str) -> dict:
    h = hashlib.sha256()
    with open(pdf, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return {
        "pages": pages or "",
        "sha256": h.hexdigest(),
        "input": pdf,
        "lang_out": lang_out,
    }


def _load_stamp() -> dict | None:
    p = WORK / STAMP
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except (OSError, json.JSONDecodeError):
        return None


def _archive_previous(old: dict) -> Path:
    """Move artifacts of a different-input collect run into archive/."""
    ts = time.strftime("%Y%m%d-%H%M%S")
    tag = str(old.get("sha256", "unknown"))[:8]
    dest = WORK / "archive" / f"{tag}_{ts}"
    dest.mkdir(parents=True, exist_ok=True)
    moved = []
    for name in _COLLECT_ARTIFACTS:
        p = WORK / name
        if p.exists():
            shutil.move(str(p), str(dest / name))
            moved.append(name)
    for sl in WORK.glob("zh_slice_*.json"):
        shutil.move(str(sl), str(dest / sl.name))
        moved.append(sl.name)
    # stale engine intermediates of the previous input
    shutil.rmtree(WORK / "working_collect", ignore_errors=True)
    print(f"[me] input fingerprint changed - archived {len(moved)} file(s) "
          f"to {dest}")
    return dest


def main() -> int:
    if len(sys.argv) < 3 or sys.argv[1] not in ("collect", "replay"):
        print(__doc__)
        return 2
    mode = sys.argv[1]
    pdf = os.path.abspath(sys.argv[2])
    pages = sys.argv[3] if len(sys.argv) > 3 else None
    if not os.path.exists(pdf):
        print(f"PDF not found: {pdf}")
        return 2

    lang_in = os.environ.get("ME_LANG_IN", "en")
    lang_out = os.environ.get("ME_LANG_OUT", "zh")

    # Windows MAX_PATH guard: the pipeline mirrors the input stem inside
    # working_dir and output filenames; long paths fail at save time.
    if len(pdf) + 120 > 255:
        print(
            "[me] WARNING: input path is very long; BabelDOC will fail to "
            "save outputs on Windows. Copy the PDF to a short name first "
            "(e.g. C:\\tmp\\book.pdf)."
        )

    fp = _fingerprint(pdf, pages, lang_out)
    prev = _load_stamp()

    if mode == "collect":
        if prev is not None:
            if prev.get("sha256") != fp["sha256"]:
                _archive_previous(prev)
                prev = None
            else:
                print(
                    "[me] same input detected - appending to the existing "
                    "collect (incremental: earlier prompts/translations kept)"
                )
        elif (WORK / "prompts.jsonl").exists():
            # Artifacts without a stamp = a collect that never finished.
            # Its sources cannot have been shipped (stamp is written at
            # collect end, before export/translate), so archiving is safe.
            _archive_previous({"sha256": "incomplete"})
    else:  # replay
        if prev is None:
            print(f"[me] ERROR: no collect run found ({STAMP} missing).")
            return 2
        mismatches = []
        if prev.get("pages") != fp["pages"]:
            mismatches.append(
                f"page range: collect={prev.get('pages') or 'ALL'} "
                f"replay={fp['pages'] or 'ALL'}"
            )
        if prev.get("sha256") != fp["sha256"]:
            mismatches.append(
                "input file content differs from the collected one "
                f"(collect={prev.get('input')})"
            )
        if prev.get("lang_out") != fp["lang_out"]:
            mismatches.append(
                f"target language: collect={prev.get('lang_out')} "
                f"replay={fp['lang_out']}"
            )
        if mismatches:
            print("[me] ERROR: collect/replay mismatch - keys would not match:")
            for m in mismatches:
                print(f"       - {m}")
            print("       Re-run collect with this input, or replay with the "
                  "collected input/range/language.")
            return 2
        if prev.get("input") != fp["input"]:
            print("[me] NOTE: path differs from collect but content matches - OK")
        # allowed incremental case: replay MUST use the collected range
        # (pages equality already checked above)

    import logging

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stdout,
    )

    WORK.mkdir(parents=True, exist_ok=True)
    os.environ["ME_MODE"] = mode
    os.environ["ME_WORK_DIR"] = str(WORK)

    # Fresh logs for this run so stale misses don't pollute the report.
    for name in ("missing.jsonl", "missing_fallback.jsonl", "unparsed.jsonl"):
        p = WORK / name
        if mode == "replay" and p.exists():
            p.unlink()

    from babeldoc.docvision.doclayout import DocLayoutModel
    from babeldoc.format.pdf.high_level import translate
    from babeldoc.format.pdf.translation_config import (
        TranslationConfig,
        WatermarkOutputMode,
    )
    from babeldoc.translator.translator import set_translate_rate_limiter

    from me_translator import MeTranslator

    # Runtime z-order fix for ocr_workaround masks (in-memory only).
    import ocr_patch

    patch_on = ocr_patch.apply()
    print(f"[me] ocr rect-order patch (ME_FIX_RECT_ORDER): {'ON' if patch_on else 'OFF'}")

    qps = 8
    set_translate_rate_limiter(qps)

    print("[me] loading layout model (first run downloads assets)...")
    doc_layout_model = DocLayoutModel.load_available()

    translator = MeTranslator(
        lang_in=lang_in, lang_out=lang_out, ignore_cache=True
    )

    output_dir = WORK / f"out_{mode}"
    cfg = TranslationConfig(
        input_file=pdf,
        translator=translator,
        lang_in=lang_in,
        lang_out=lang_out,
        pages=pages,
        doc_layout_model=doc_layout_model,
        output_dir=str(output_dir),
        working_dir=str(WORK / f"working_{mode}"),
        watermark_output_mode=WatermarkOutputMode.NoWatermark,
        auto_extract_glossary=False,  # keep prompts deterministic & avoid using
        #                                 MeTranslator for term extraction
        ocr_workaround=os.environ.get("ME_OCR_WORKAROUND") == "1",
        qps=qps,
        pool_max_workers=qps,
        debug=False,
    )

    print(
        f"[me] mode={mode}  input={pdf}  pages={pages or 'all'}  "
        f"lang={lang_in}->{lang_out}",
        flush=True,
    )
    translate(cfg)

    if mode == "collect":
        (WORK / STAMP).write_text(
            json.dumps(fp, ensure_ascii=False, indent=1), encoding="utf-8"
        )

    print(f"[me] done -> {output_dir}")
    print(
        f"[me] prompts={_count_lines(WORK / 'prompts.jsonl')}  "
        f"fallback_src={_count_lines(WORK / 'fallback_src.jsonl')}  "
        f"missing_llm={_count_lines(WORK / 'missing.jsonl')}  "
        f"missing_fb={_count_lines(WORK / 'missing_fallback.jsonl')}  "
        f"unparsed={_count_lines(WORK / 'unparsed.jsonl')}"
    )
    if mode == "replay" and translator.missing_llm_count + translator.missing_fb_count:
        print("[me] WARNING: some segments were not covered by the mapping files.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
