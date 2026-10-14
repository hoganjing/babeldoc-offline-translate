"""Advisory pre-scan of a PDF before a BabelDOC collect run.

Usage:

  python preflight.py <pdf>

Read-only collect-free scan. Prints absolute path, file size (MB), page
count, per-page char counts via PyMuPDF, the list of zero-text pages and
of image-dominant pages (chars < 200 AND a single image covering >80% of
the page), then RECOMMEND: lines and a babeldoc version check.

Always exits 0 (advisory only); exit 2 when the PDF argument is missing
or the file cannot be opened. Never writes to the work dir.
"""

from __future__ import annotations

import importlib.metadata
import os
import sys
from pathlib import Path

import fitz

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

WORK = Path(os.environ.get("ME_WORK_DIR") or (Path.cwd() / "me_work")).resolve()

USAGE = "usage: python preflight.py <pdf>"

IMAGE_COVER = 0.8  # fraction of page area for a "full-page" image
IMAGE_DOMINANT_CHARS = 200  # chars below this + full-page image = scanned


def image_covers_page(page: fitz.Page) -> bool:
    """True when one image bbox covers >80% of the page area."""
    page_area = float(page.rect.width) * float(page.rect.height)
    if page_area <= 0:
        return False
    for info in page.get_image_info():
        bbox = fitz.Rect(info["bbox"]) & page.rect
        if bbox.is_empty or bbox.width <= 0 or bbox.height <= 0:
            continue
        if (float(bbox.width) * float(bbox.height)) / page_area > IMAGE_COVER:
            return True
    return False


def check_babeldoc_version() -> None:
    """Print a WARNING if babeldoc is missing or off the 0.6.x contract."""
    try:
        ver = importlib.metadata.version("babeldoc")
    except importlib.metadata.PackageNotFoundError:
        print("WARNING: babeldoc not installed")
        return
    if not ver.startswith("0.6"):
        print(
            f"WARNING: babeldoc {ver} - skill contract verified against 0.6.x;"
            " if prompts go unparsed, check markers in me_translator.py"
        )


def page_metrics(page: fitz.Page) -> tuple[int, bool]:
    """(char count, image-dominant?) for one page."""
    chars = len(page.get_text())
    dominant = chars < IMAGE_DOMINANT_CHARS and image_covers_page(page)
    return chars, dominant


def _fmt_pages(pages: list[int]) -> str:
    return ", ".join(str(n) for n in pages) if pages else "none"


def main() -> int:
    if len(sys.argv) != 2:
        print(USAGE)
        return 2

    raw = sys.argv[1]
    try:
        path = Path(raw).resolve()
    except OSError:
        print(f"ERROR: cannot resolve path: {raw}")
        return 2
    if not path.is_file():
        print(f"ERROR: PDF not found or not a file: {raw}")
        return 2
    try:
        doc = fitz.open(path)
    except Exception as exc:  # noqa: BLE001 - advisory script, any open error
        print(f"ERROR: cannot open PDF: {path} ({exc})")
        return 2
    with doc:
        if doc.needs_pass and not doc.authenticate(""):
            print(f"ERROR: PDF is encrypted and needs a password: {path}")
            return 2

        n_pages = doc.page_count
        print(f"pdf: {path}")
        print(f"size: {path.stat().st_size / (1024 * 1024):.2f} MB")
        print(f"pages: {n_pages}")

        total_chars = 0
        zero_pages: list[int] = []
        dominant_pages: list[int] = []
        for i in range(1, n_pages + 1):
            try:
                chars, dominant = page_metrics(doc[i - 1])
            except Exception as exc:  # noqa: BLE001 - keep scanning
                print(f"ERR page {i}: {exc}")
                continue
            total_chars += chars
            if chars == 0:
                zero_pages.append(i)
            if dominant:
                dominant_pages.append(i)

        print(f"summary: {n_pages} pages, {total_chars} chars total")
        print(f"pages with no text layer (cannot be translated): {_fmt_pages(zero_pages)}")
        print(
            "image-dominant pages (chars < 200 + full-page image): "
            f"{_fmt_pages(dominant_pages)}"
        )
        print(f"counts: zero-text={len(zero_pages)} image-dominant={len(dominant_pages)}")

        if dominant_pages:
            print(
                "RECOMMEND: likely scanned/image-heavy PDF - if collect fails"
                " with ScannedPDFError, re-run with ME_OCR_WORKAROUND=1"
                " (collect AND replay)"
            )
        if zero_pages:
            print(
                "RECOMMEND: page(s) "
                f"{_fmt_pages(zero_pages)} have no text layer and will stay"
                " untranslated (covers/titles)"
            )
        if len(str(path)) > 200:
            print(
                "RECOMMEND: input path is very long - copy the PDF to a short"
                " ASCII filename first (Windows MAX_PATH)"
            )
        if sys.platform == "win32":
            print(
                "RECOMMEND: set PYTHONUTF8=1 before running collect/replay"
                " on Windows"
            )
        print("RECOMMEND: next step - run_translation.py collect <pdf> [pages]")

        check_babeldoc_version()
    return 0


if __name__ == "__main__":
    sys.exit(main())
