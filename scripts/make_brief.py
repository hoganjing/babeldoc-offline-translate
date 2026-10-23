#!/usr/bin/env python3
"""Generate a per-book WORKER_BRIEF.md from the skill template.

Fills the placeholders in ``templates/worker_brief.md``:
  {{BOOK_TITLE}}      book title (header)
  {{BOOK_META}}       one-line meta (authors, year, pages, domain)
  {{WORK_DIR}}        absolute work dir (forward slashes)
  {{SPECIAL_TERMS}}   curated glossary terms, or a fallback sentence

Usage:
  python make_brief.py --work <dir> --book "<title>" [--meta "<meta>"]
                      [--terms path-or-inline] [--out <path>]
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

SCRIPT_DIR = Path(__file__).resolve().parent
TEMPLATE = SCRIPT_DIR.parent / "templates" / "worker_brief.md"


def load_terms(arg: str | None) -> str | None:
    if not arg:
        return None
    p = Path(arg)
    if p.is_file():
        lines = [
            ln.strip()
            for ln in p.read_text(encoding="utf-8").splitlines()
            if ln.strip()
        ]
    else:
        lines = [ln.strip() for ln in arg.replace(";", "\n").splitlines() if ln.strip()]
    return "; ".join(lines) + "。" if lines else None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--work", required=True, help="work dir (default output location)")
    ap.add_argument("--book", required=True, help="book title for the brief header")
    ap.add_argument(
        "--meta", default="", help="one-line book meta (authors, year, pages, domain)"
    )
    ap.add_argument(
        "--terms",
        default=None,
        help="path to a terms file (one 'src→tgt' per line) OR inline 'src→tgt' list",
    )
    ap.add_argument(
        "--out",
        default=None,
        help="output path (default <work>/WORKER_BRIEF.md)",
    )
    args = ap.parse_args()

    work = Path(args.work).resolve()
    work_dir = str(work).replace("\\", "/")
    if not TEMPLATE.exists():
        print(f"ERROR: template not found: {TEMPLATE}")
        return 2
    text = TEMPLATE.read_text(encoding="utf-8")

    terms = load_terms(args.terms)
    special = (
        terms
        if terms
        else "（本书未提供专项术语表；按领域通行译法自行决断并保持全书一致。）"
    )

    text = (
        text.replace("{{BOOK_TITLE}}", args.book)
        .replace("{{BOOK_META}}", args.meta)
        .replace("{{WORK_DIR}}", work_dir)
        .replace("{{SPECIAL_TERMS}}", special)
    )

    out = Path(args.out).resolve() if args.out else (work / "WORKER_BRIEF.md")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")
    print(f"[me] wrote worker brief -> {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
