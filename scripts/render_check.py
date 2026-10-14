"""Render selected pages of a PDF to PNG for visual verification.

Usage:

  python render_check.py <pdf> <pages> [--dpi N] [--outdir DIR]

<pages>  comma-separated 1-based page numbers (e.g. 1,9,12) or the literal
         'auto' = first, middle and last page (deduplicated, clamped into
         range).
--dpi    render resolution, default 100
--outdir output directory, default 'render_check' relative to the current
         directory (created if needed)

Writes <stem>_p<N>.png per page, prints one absolute PNG path per line,
then 'rendered: k pages'. Exit 2 on wrong arguments, a missing/unreadable
PDF, or invalid page numbers.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import fitz

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

WORK = Path(os.environ.get("ME_WORK_DIR") or (Path.cwd() / "me_work")).resolve()

USAGE = "usage: python render_check.py <pdf> <pages> [--dpi N] [--outdir DIR]"
DEFAULT_DPI = 100
DEFAULT_OUTDIR = "render_check"


def parse_cli(argv: list[str]) -> tuple[list[str], int, str] | None:
    """Parse positionals + --dpi/--outdir. Returns (positional, dpi, outdir)."""
    positional: list[str] = []
    dpi = DEFAULT_DPI
    outdir = DEFAULT_OUTDIR
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg in ("--dpi", "--outdir"):
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
    return positional, dpi, outdir


def parse_pages(spec: str, page_count: int) -> list[int] | None:
    """Parse the <pages> argument; None + message on invalid input."""
    if page_count < 1:
        print(f"ERROR: PDF has no pages (page_count={page_count})")
        return None
    if spec == "auto":
        wanted = [1, (page_count + 1) // 2, page_count]
        pages: list[int] = []
        for n in wanted:
            n = min(max(n, 1), page_count)  # clamp into range
            if n not in pages:
                pages.append(n)
        return pages
    pages = []
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
    parsed = parse_cli(sys.argv[1:])
    if parsed is None:
        return 2
    positional, dpi, outdir = parsed
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
    with doc:
        if doc.needs_pass and not doc.authenticate(""):
            print(f"ERROR: PDF is encrypted and needs a password: {pdf_path}")
            return 2
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
    print(f"rendered: {len(written)} pages")
    return 0


if __name__ == "__main__":
    sys.exit(main())
