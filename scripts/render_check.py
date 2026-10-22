"""Render selected pages of a PDF to PNG for visual verification.

Usage:

  python render_check.py <pdf> <pages> [--dpi N] [--outdir DIR] [--auto-count N]

<pages>  comma-separated 1-based page numbers (e.g. 1,9,12) or the literal
         'auto'.
--dpi    render resolution, default 100
--outdir output directory, default 'render_check' relative to the current
         directory (created if needed)
--auto-count  number of text-rich pages to sample in auto mode (default 6)

'auto' is text-aware instead of "first, middle, last":

  * pages with fewer than 200 characters are skipped (covers, blank pages,
    part-title separators) - rendering those wastes the visual check;
  * the remaining pages are sampled evenly (first, last and quantiles), so the
    sample spans the whole document;
  * up to two pages that begin mid-sentence (lowercase or continuation
    punctuation after a previous page that does not end a sentence) are added:
    those are the cross-page paragraph merges where layout damage shows.

Writes <stem>_p<N>.png per page, prints one absolute PNG path per line,
then 'rendered: k pages'. Exit 2 on wrong arguments, a missing/unreadable
PDF, or invalid page numbers.
"""

from __future__ import annotations

import sys
from pathlib import Path

try:
    import fitz  # PyMuPDF
except ImportError:  # pragma: no cover - depends on the environment
    fitz = None

FITZ_HINT = "PyMuPDF (fitz) is required to render pages: python -m pip install pymupdf"

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

USAGE = (
    "usage: python render_check.py <pdf> <pages> [--dpi N] [--outdir DIR] "
    "[--auto-count N]"
)
DEFAULT_DPI = 100
DEFAULT_OUTDIR = "render_check"
DEFAULT_AUTO_COUNT = 6
MIN_AUTO_CHARS = 200

# A page starting with one of these continues the previous page's sentence.
CONTINUATION_START = ",;:，、；：）)]」』"
# A previous page ending with one of these completed its sentence.
SENTENCE_END = ".?!。？！:;\"')]”’"


def parse_cli(argv: list[str]) -> tuple[list[str], int, str, int] | None:
    """Parse positionals + options. Returns (positional, dpi, outdir, count)."""
    positional: list[str] = []
    dpi = DEFAULT_DPI
    outdir = DEFAULT_OUTDIR
    auto_count = DEFAULT_AUTO_COUNT
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg in ("--dpi", "--outdir", "--auto-count"):
            if i + 1 >= len(argv):
                print(f"ERROR: {arg} needs a value")
                print(USAGE)
                return None
            value = argv[i + 1]
            if arg == "--dpi":
                try:
                    dpi = int(value)
                except ValueError:
                    print(f"ERROR: --dpi must be an integer, got {value!r}")
                    print(USAGE)
                    return None
                if dpi < 1:
                    print(f"ERROR: --dpi must be >= 1, got {dpi}")
                    print(USAGE)
                    return None
            elif arg == "--auto-count":
                try:
                    auto_count = int(value)
                except ValueError:
                    print(f"ERROR: --auto-count must be an integer, got {value!r}")
                    print(USAGE)
                    return None
                if not 1 <= auto_count <= 50:
                    print(f"ERROR: --auto-count must be 1..50, got {auto_count}")
                    print(USAGE)
                    return None
            else:
                outdir = value
            i += 2
        elif arg.startswith("-"):
            print(f"ERROR: unknown option {arg}")
            print(USAGE)
            return None
        else:
            positional.append(arg)
            i += 1
    return positional, dpi, outdir, auto_count


def continuation_pages(doc: fitz.Document) -> list[int]:
    """Pages that start mid-sentence, i.e. a paragraph split across the break."""
    pages: list[int] = []
    previous_end = ""
    for index in range(doc.page_count):
        text = doc[index].get_text().strip()
        if not text:
            previous_end = ""
            continue
        first = text[0]
        if previous_end and (
            first.islower() or first in CONTINUATION_START
        ) and previous_end not in SENTENCE_END:
            pages.append(index + 1)
        previous_end = text[-1]
    return pages


def auto_pages(
    doc: fitz.Document, count: int, continuation: list[int]
) -> tuple[list[int], dict]:
    """Text-aware sample: skip short pages, spread picks, append continuations."""
    lengths = [len(doc[i].get_text().strip()) for i in range(doc.page_count)]
    content = [i + 1 for i, n in enumerate(lengths) if n >= MIN_AUTO_CHARS]
    if not content:
        content = [i + 1 for i, n in enumerate(lengths) if n > 0] or list(
            range(1, doc.page_count + 1)
        )
    picks: list[int] = []

    def add(page: int) -> None:
        if page not in picks:
            picks.append(page)

    k = min(count, len(content))
    if k == 1:
        add(content[len(content) // 2])
    else:
        for j in range(k):
            add(content[round(j * (len(content) - 1) / (k - 1))])

    added: list[int] = []
    for page in continuation:
        if page in picks or not 1 <= page <= doc.page_count:
            continue
        if lengths[page - 1] < 50:
            continue
        add(page)
        added.append(page)
        if len(added) == 2:
            break

    picks.sort()
    stats = {
        "content_pages": len(content),
        "total_pages": doc.page_count,
        "skipped_short": doc.page_count - len(content),
        "continuations": continuation,
        "continuations_added": added,
    }
    return picks, stats


def parse_pages(spec: str, page_count: int) -> list[int] | None:
    """Parse an explicit <pages> list; None + message on invalid input."""
    if page_count < 1:
        print(f"ERROR: PDF has no pages (page_count={page_count})")
        return None
    pages: list[int] = []
    for part in spec.split(","):
        part = part.strip()
        try:
            n = int(part)
        except ValueError:
            print(f"ERROR: invalid page number {part!r} in {spec!r}")
            print(USAGE)
            return None
        if n < 1 or n > page_count:
            print(f"ERROR: page {n} out of range 1..{page_count}")
            return None
        if n not in pages:
            pages.append(n)
    if not pages:
        print(f"ERROR: no pages selected in {spec!r}")
        return None
    return pages


def render_pages(
    pdf_path: Path, pages: list[int], dpi: int, out_dir: Path
) -> list[Path]:
    """Render each page to <outdir>/<stem>_p<N>.png; return written paths."""
    out_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    with fitz.open(pdf_path) as doc:
        for n in pages:
            pix = doc[n - 1].get_pixmap(dpi=dpi)
            out = out_dir / f"{pdf_path.stem}_p{n}.png"
            pix.save(str(out))
            written.append(out.resolve())
    return written


def main() -> int:
    if fitz is None:
        print(f"ERROR: {FITZ_HINT}")
        return 2
    parsed = parse_cli(sys.argv[1:])
    if parsed is None:
        return 2
    positional, dpi, outdir, auto_count = parsed
    if len(positional) != 2:
        print(USAGE)
        return 2
    raw_pdf, pages_spec = positional

    try:
        pdf_path = Path(raw_pdf).resolve()
    except OSError:
        print(f"ERROR: cannot resolve path: {raw_pdf}")
        return 2
    if not pdf_path.is_file():
        print(f"ERROR: PDF not found or not a file: {raw_pdf}")
        return 2
    try:
        doc = fitz.open(pdf_path)
    except Exception as exc:  # noqa: BLE001 - surface any open error
        print(f"ERROR: cannot open PDF: {pdf_path} ({exc})")
        return 2

    auto_stats = None
    with doc:
        if doc.needs_pass and not doc.authenticate(""):
            print(f"ERROR: PDF is encrypted and needs a password: {pdf_path}")
            return 2
        if pages_spec == "auto":
            pages, auto_stats = auto_pages(
                doc, auto_count, continuation_pages(doc)
            )
        else:
            pages = parse_pages(pages_spec, doc.page_count)
            if pages is None:
                return 2

    out_dir = Path(outdir)  # relative names resolve against the cwd
    try:
        written = render_pages(pdf_path, pages, dpi, out_dir)
    except OSError as exc:
        print(f"ERROR: cannot write PNG to {out_dir}: {exc}")
        return 2
    for png in written:
        print(png)
    if auto_stats:
        print(
            "auto: "
            f"{len(pages)} pages from {auto_stats['content_pages']} text-rich "
            f"(of {auto_stats['total_pages']}, skipped {auto_stats['skipped_short']} short)"
            f"; continuation pages={auto_stats['continuations'] or 'none'}, "
            f"added={auto_stats['continuations_added'] or 'none'}"
        )
    print(f"rendered: {len(written)} pages")
    return 0


if __name__ == "__main__":
    sys.exit(main())
