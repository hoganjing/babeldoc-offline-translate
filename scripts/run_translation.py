"""Two-phase BabelDOC driver using MeTranslator (offline agent translation).

Usage:
  python run_translation.py collect <input.pdf> [page_range e.g. 1-5]
  python run_translation.py replay  <input.pdf> [page_range]
      [--allow-misses] [--precheck-only] [--log-file PATH]

--log-file tees everything (stdout + stderr + babeldoc logs) into a UTF-8 file;
a relative path is resolved inside the work dir. Use it on Windows when the
console codepage is not UTF-8, or to keep a record of a long replay.

Replay runs a coverage preflight before loading the layout model: prompts whose
translation is in neither llm_outputs.json nor text_index.json abort the run
with exit 3, instead of being discovered after tens of minutes of rendering.
--precheck-only prints the prediction and exits; --allow-misses overrides.

IMPORTANT: collect and replay MUST use the same input file (same content)
and the same page range, otherwise the batch keys will not match. The driver
enforces this via <work>/run_stamp.json which records the page range, a sha256
fingerprint of the PDF, and both languages.

Collect archives the previous run when the input, either language, or a
page range that is not a strict extension changes. A page-range extension
keeps the earlier prompts/translations (incremental top-up); a shrink or an
unparseable change starts a clean collect so stale out-of-range prompts cannot
leak into the next merge.

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
  ME_LANG_IN / ME_LANG_OUT   source/target language (default en / zh).
                       Must match between collect and replay (enforced).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
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
    "text_index.json",
    "fallback_outputs.json",
    "fallback_src.jsonl",
    "retry.jsonl",
    "coverage_missing.jsonl",
    "coverage_report.json",
    "chunks_meta.json",
    "chunks_retry_meta.json",
    "chunks_fix_meta.json",
    "content_warnings.jsonl",
    "glossary_warnings.jsonl",
    STAMP,
)
# Chunk/agent outputs and replay miss logs are also input-specific.
_COLLECT_GLOBS = (
    "chunk_*.json",
    "llm_chunk_*.json",
    "zh_slice_*.json",
    "missing*.jsonl",
    "unparsed.jsonl",
)
_COLLECT_DIRS = ("out_collect", "out_replay")


def _count_lines(path: Path) -> int:
    if not path.exists():
        return 0
    with path.open(encoding="utf-8") as f:
        return sum(1 for _ in f)


def _fingerprint(
    pdf: str, pages: str | None, lang_in: str, lang_out: str
) -> dict:
    h = hashlib.sha256()
    with open(pdf, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return {
        "pages": pages or "",
        "sha256": h.hexdigest(),
        "input": pdf,
        "lang_in": lang_in,
        "lang_out": lang_out,
    }


def _page_set(pages: str | None) -> set[int] | None:
    """Parse a simple page spec (``1-5,9`` or ``all``) into page numbers.

    ``None`` means every page. Raises ``ValueError`` when the spec is not a
    comma-separated list of positive integers / integer ranges, so callers can
    treat it conservatively instead of guessing.
    """
    spec = (pages or "").strip().lower()
    if not spec or spec == "all":
        return None
    result: set[int] = set()
    for part in spec.split(","):
        part = part.strip()
        if not part:
            raise ValueError(f"empty page range component in {pages!r}")
        match = re.fullmatch(r"(\d+)(?:\s*-\s*(\d+))?", part)
        if match is None:
            raise ValueError(f"unsupported page range component {part!r}")
        start = int(match.group(1))
        end = int(match.group(2) or start)
        if start < 1 or end < start:
            raise ValueError(f"invalid page range component {part!r}")
        result.update(range(start, end + 1))
    if not result:
        raise ValueError(f"empty page range in {pages!r}")
    return result


def _range_extends(old: str | None, new: str | None) -> bool | None:
    """True when ``new`` covers every page of ``old``; None if undecidable.

    ``None`` as a page spec means ALL pages. An undecidable pair (unparseable
    syntax) must be treated as a change, never silently as an extension.
    """
    try:
        old_set = _page_set(old)
        new_set = _page_set(new)
    except ValueError:
        return None
    if new_set is None:
        return True
    if old_set is None:
        return False
    return old_set <= new_set


def _collect_action(prev: dict | None, fp: dict) -> str:
    """What a collect run does with the previous work dir.

    ``fresh``   no usable previous run
    ``append``  same input and languages, page range unchanged or extended
    ``archive`` different input/languages, or a page range that is not a
                strict extension (shrink / arbitrary change / unparseable)
    """
    if prev is None:
        return "fresh"
    # Stamps written before lang_in was recorded: assume it did not change.
    prev_lang_in = prev.get("lang_in")
    if prev_lang_in is None:
        prev_lang_in = fp["lang_in"]
    if (
        prev.get("sha256") != fp["sha256"]
        or prev_lang_in != fp["lang_in"]
        or prev.get("lang_out") != fp["lang_out"]
    ):
        return "archive"
    if prev.get("pages") != fp["pages"]:
        if _range_extends(prev.get("pages"), fp["pages"]) is not True:
            return "archive"
    return "append"


def _load_stamp() -> dict | None:
    p = WORK / STAMP
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8-sig"))
        return data if isinstance(data, dict) else None
    except (OSError, json.JSONDecodeError):
        return None


def _archive_previous(old: dict, reason: str = "input fingerprint changed") -> Path:
    """Move artifacts of a different-input collect run into archive/."""
    ts = time.strftime("%Y%m%d-%H%M%S")
    tag = str(old.get("sha256", "unknown"))[:8]
    dest = WORK / "archive" / f"{tag}_{ts}"
    dest.mkdir(parents=True, exist_ok=True)
    moved = []

    def move_file(p: Path) -> None:
        try:
            shutil.move(str(p), str(dest / p.name))
            moved.append(p.name)
        except OSError as e:
            print(f"[me] WARNING: could not archive {p.name}: {e}")

    for name in _COLLECT_ARTIFACTS:
        p = WORK / name
        if p.exists():
            move_file(p)
    for pattern in _COLLECT_GLOBS:
        for p in sorted(WORK.glob(pattern)):
            if p.is_file():
                move_file(p)
    for name in _COLLECT_DIRS:
        p = WORK / name
        if p.is_dir():
            try:
                shutil.move(str(p), str(dest / name))
                moved.append(name + "/")
            except OSError as e:
                print(f"[me] WARNING: could not archive {name}: {e}")
    # stale engine intermediates of the previous input
    for name in ("working_collect", "working_replay"):
        shutil.rmtree(WORK / name, ignore_errors=True)
    print(f"[me] {reason} - archived {len(moved)} item(s) to {dest}")
    return dest


def _tee_log(path: Path, work: Path) -> Path:
    """Mirror stdout/stderr into a UTF-8 log file (Windows-safe, no codepage loss)."""
    if not path.is_absolute():
        path = work / path
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("w", encoding="utf-8", buffering=1)

    class Tee:
        def __init__(self, stream):
            self.stream = stream

        def write(self, data):
            self.stream.write(data)
            handle.write(data)
            return len(data)

        def flush(self):
            self.stream.flush()
            handle.flush()

        def isatty(self):
            return False

    sys.stdout = Tee(sys.stdout)
    sys.stderr = Tee(sys.stderr)
    return path


def _safe_stem(stem: str) -> str:
    """A filename BabelDOC will mirror into its outputs, short enough to save."""
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", stem).strip(" .")
    if len(cleaned) > 80:
        cleaned = cleaned[:80].rstrip(" .")
    return cleaned or "book"


def _clean_pdfs(directory: Path) -> int:
    """Delete *.pdf in one output dir; return how many were removed.

    Output names mirror the input stem and the target language, so a previous
    run with another language/name can leave PDFs that would otherwise be
    copied back as if they belonged to the current run.
    """
    if not directory.is_dir():
        return 0
    removed = 0
    for path in directory.glob("*.pdf"):
        try:
            path.unlink()
            removed += 1
        except OSError as exc:
            print(f"[me] WARNING: could not remove stale {path.name}: {exc}")
    return removed


def _short_run(pdf: str, digest: str, mode: str):
    """Copy the input to a short temp path; inputs/outputs/working dirs all short.

    BabelDOC mirrors the input stem into output names and engine intermediates,
    so only shortening the input is not enough on Windows.
    """
    if os.name != "nt":
        return None
    stem = _safe_stem(Path(pdf).stem)
    base = Path(tempfile.gettempdir()) / "me_short" / digest[:8]
    short_pdf = base / f"{stem}.pdf"
    try:
        base.mkdir(parents=True, exist_ok=True)
        # The scratch output dir is reused for the same input digest. Clear it
        # so a previous language/name cannot be restored into this run.
        _clean_pdfs(base / f"out_{mode}")
        if not short_pdf.exists() or short_pdf.stat().st_size != os.path.getsize(pdf):
            shutil.copy2(pdf, short_pdf)
    except OSError as exc:
        print(f"[me] WARNING: short-path copy failed ({exc}); using original path")
        return None
    return str(short_pdf), base / f"out_{mode}", base / f"working_{mode}"


def _restore_outputs(src_dir: Path, dest_dir: Path) -> int:
    """Copy rendered PDFs back under their normal names (long-path safe)."""
    if not src_dir.exists():
        return 0
    dest_dir.mkdir(parents=True, exist_ok=True)
    copied = 0
    for src in src_dir.glob("*.pdf"):
        target = str(dest_dir / src.name)
        if os.name == "nt" and len(target) >= 250 and not target.startswith("\\\\?\\"):
            target = "\\\\?\\" + target
        shutil.copy2(src, target)
        copied += 1
    return copied


def main() -> int:
    argv = sys.argv[1:]
    positional: list[str] = []
    flags: set[str] = set()
    log_file: str | None = None
    unknown: list[str] = []
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg == "--log-file":
            if i + 1 >= len(argv):
                print("ERROR: --log-file needs a path")
                return 2
            log_file = argv[i + 1]
            i += 2
        elif arg in ("--allow-misses", "--precheck-only"):
            flags.add(arg)
            i += 1
        elif arg.startswith("--"):
            unknown.append(arg)
            i += 1
        else:
            positional.append(arg)
            i += 1
    if unknown:
        print(f"unknown option(s): {', '.join(sorted(unknown))}")
        print(__doc__)
        return 2
    if len(positional) < 2 or positional[0] not in ("collect", "replay"):
        print(__doc__)
        return 2
    mode = positional[0]
    pdf = os.path.abspath(positional[1])
    pages = positional[2] if len(positional) > 2 else None
    allow_misses = "--allow-misses" in flags
    precheck_only = "--precheck-only" in flags
    if precheck_only and mode != "replay":
        print("--precheck-only is only valid with replay")
        return 2
    if log_file:
        log_path = _tee_log(Path(log_file), WORK)
        print(f"[me] log -> {log_path}")
    if not os.path.exists(pdf):
        print(f"PDF not found: {pdf}")
        return 2

    lang_in = os.environ.get("ME_LANG_IN", "en")
    lang_out = os.environ.get("ME_LANG_OUT", "zh")

    fp = _fingerprint(pdf, pages, lang_in, lang_out)
    prev = _load_stamp()

    # Windows MAX_PATH guard: BabelDOC mirrors the input stem inside
    # working_dir and output filenames; long paths fail at save time. Run from
    # a short temp copy and copy the rendered PDFs back afterwards.
    short = None
    if os.name == "nt" and len(pdf) + 120 > 255:
        short = _short_run(pdf, fp["sha256"], mode)
        if short:
            print(f"[me] long path detected - running via short copy: {short[0]}")
            print(f"[me] outputs will be copied back to {WORK / ('out_' + mode)}")
        else:
            print(
                "[me] WARNING: input path is very long and the short copy "
                "failed; saving outputs may fail on Windows."
            )

    if mode == "collect":
        if prev is not None:
            action = _collect_action(prev, fp)
            if action == "archive":
                if prev.get("sha256") != fp["sha256"]:
                    reason = "input changed"
                elif prev.get("lang_out") != fp["lang_out"]:
                    reason = "target language changed"
                elif prev.get("lang_in") not in (None, fp["lang_in"]):
                    reason = "source language changed"
                else:
                    reason = (
                        f"page range changed ({prev.get('pages') or 'ALL'} -> "
                        f"{fp['pages'] or 'ALL'}); not an extension"
                    )
                _archive_previous(prev, reason)
                prev = None
            else:
                print(
                    "[me] same input detected - appending to the existing "
                    "collect (incremental: earlier prompts/translations kept)"
                )
                if prev.get("pages") != fp["pages"]:
                    print(
                        f"[me] page range extended ({prev.get('pages') or 'ALL'} "
                        f"-> {fp['pages'] or 'ALL'}) - keeping earlier prompts"
                    )
        elif (WORK / "prompts.jsonl").exists():
            # Artifacts without a stamp = a collect that never finished.
            # Its sources cannot have been shipped (stamp is written at
            # collect end, before export/translate), so archiving is safe.
            _archive_previous({"sha256": "incomplete"}, "unfinished collect")
    else:  # replay
        if prev is None:
            print(f"[me] ERROR: no collect run found ({STAMP} missing).")
            return 2
        # If no page range was passed, default to the range collect recorded,
        # so a replay invoked without an explicit range can never mismatch on
        # pages (collect may have recorded an explicit range like "1-910").
        if pages is None:
            pages = prev.get("pages") or ""
            print(
                f"[me] replay: no page range passed - using collected range "
                f"'{pages or 'ALL'}'"
            )
            fp = _fingerprint(pdf, pages, lang_in, lang_out)
        mismatches = []
        prev_pages = prev.get("pages") or ""
        if prev_pages != fp["pages"]:
            mismatches.append(
                f"page range: collect={prev_pages or 'ALL'} "
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
        # Legacy stamps have no lang_in; assume it was unchanged.
        prev_lang_in = prev.get("lang_in", fp["lang_in"])
        if prev_lang_in != fp["lang_in"]:
            mismatches.append(
                f"source language: collect={prev_lang_in} "
                f"replay={fp['lang_in']}"
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

        from replay_precheck import format_report, precheck

        try:
            result = precheck(WORK)
        except FileNotFoundError as exc:
            print(f"[me] ERROR: {exc}")
            return 2
        except ValueError as exc:
            print(f"[me] ERROR: {exc}")
            return 2
        print(format_report(result))
        if result["missing"]:
            if not allow_misses:
                print(
                    "[me] ERROR: replay preflight failed - segments without a "
                    "translation would come out untranslated."
                )
                print("       Close the gaps first:")
                merge_py = SCRIPT_DIR / "pipe_merge.py"
                prep_py = SCRIPT_DIR / "pipe_prep.py"
                print(f'         python "{merge_py}" --work "{WORK}"')
                print(
                    f'         python "{prep_py}" --work "{WORK}" '
                    "--from-missing coverage_missing.jsonl"
                )
                print(
                    "       translate chunk_fix_*.json -> llm_chunk_fix_*.json, "
                    "merge again, then replay."
                )
                print(
                    "       (--allow-misses replays anyway; exact-text fallback "
                    "still applies.)"
                )
                return 3
            print("[me] WARNING: proceeding with missing translations (--allow-misses).")
        if precheck_only:
            return 0

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

    qps = 8
    set_translate_rate_limiter(qps)

    print("[me] loading layout model (first run downloads assets)...")
    doc_layout_model = DocLayoutModel.load_available()

    translator = MeTranslator(
        lang_in=lang_in, lang_out=lang_out, ignore_cache=True
    )

    output_dir = WORK / f"out_{mode}"
    working_dir = WORK / f"working_{mode}"
    input_file = pdf
    if short is not None:
        input_file, output_dir, working_dir = short
    cfg = TranslationConfig(
        input_file=input_file,
        translator=translator,
        lang_in=lang_in,
        lang_out=lang_out,
        pages=pages,
        doc_layout_model=doc_layout_model,
        output_dir=str(output_dir),
        working_dir=str(working_dir),
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

    if short is not None:
        final_out = WORK / f"out_{mode}"
        removed = _clean_pdfs(final_out)
        if removed:
            print(f"[me] removed {removed} stale PDF(s) from {final_out}")
        copied = _restore_outputs(Path(output_dir), final_out)
        print(f"[me] restored {copied} PDF(s) -> {final_out}")
        output_dir = final_out

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
        f"text_fallback={getattr(translator, 'text_fallback_count', 0)}  "
        f"unparsed={_count_lines(WORK / 'unparsed.jsonl')}"
    )
    if mode == "replay" and translator.missing_llm_count + translator.missing_fb_count:
        print("[me] WARNING: some segments were not covered by the mapping files.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
