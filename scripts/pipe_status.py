#!/usr/bin/env python3
"""One-screen status for an offline BabelDOC work dir, with cost estimates.

Answers, without running the pipeline:

  * how much has been collected (prompts, kinds, source characters);
  * how much is translated (keyed, exact-text, still missing);
  * whether the chunk files that were written have been answered;
  * how long replay/collect will take and how much memory the PDF needs.

Estimates use coefficients measured on a 429-page book (see constants below),
plus the PDF's page count. They are planning numbers (+-25%), not promises.

Usage:
  python pipe_status.py [--work DIR] [--json]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from pipe_merge import _extract_json_input
from replay_precheck import precheck

WORK = Path(
    os.environ.get("ME_WORK_DIR") or (Path.cwd() / "me_work")
).resolve()

SINGLE_MARKER = "Now translate the following text:"

# Measured on 2852 prompts / 429 pages: replay 890-1008 s, collect 1835 s,
# peak RSS 3044-3052 MB (layout model dominates).
REPLAY_FIXED_S = 40.0
REPLAY_S_PER_PAGE = 2.25
COLLECT_FIXED_S = 40.0
COLLECT_S_PER_PAGE = 4.2
MEM_BASE_MB = 2200.0
MEM_MB_PER_PAGE = 2.0

COST_RE = re.compile(r"finish translate: .*?cost: ([0-9.]+)")
MEM_RE = re.compile(r"Peak memory usage: ([0-9.]+) MB")


def _fmt_minutes(seconds: float) -> str:
    minutes = seconds / 60.0
    if minutes < 1:
        return f"{seconds:.0f}s"
    if minutes < 90:
        return f"{minutes:.0f} min"
    return f"{minutes / 60:.1f} h"


def _prompt_stats(work: Path) -> dict:
    path = work / "prompts.jsonl"
    stats = {
        "prompts": 0,
        "batch": 0,
        "single": 0,
        "source_chars": 0,
        "unparseable": 0,
    }
    if not path.exists():
        return stats
    with path.open(encoding="utf-8-sig") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except ValueError:
                stats["unparseable"] += 1
                continue
            kind = record.get("kind") or (
                "batch" if str(record.get("key", "")).startswith("b:") else "single"
            )
            if kind == "batch":
                try:
                    items = _extract_json_input(record.get("prompt", ""))
                except ValueError:
                    stats["unparseable"] += 1
                    continue
                stats["batch"] += 1
                stats["source_chars"] += sum(
                    len(item.get("input", ""))
                    for item in items
                    if isinstance(item, dict)
                )
            else:
                source = record.get("source") or ""
                if not source and record.get("prompt"):
                    prompt = record["prompt"]
                    pos = prompt.find(SINGLE_MARKER)
                    if pos != -1:
                        source = prompt[pos + len(SINGLE_MARKER):].lstrip("\n")
                stats["single"] += 1
                stats["source_chars"] += len(source)
            stats["prompts"] += 1
    return stats


def _record_chars(record: dict) -> int:
    if record.get("kind") == "batch":
        return sum(
            len(item.get("input", ""))
            for item in record.get("items") or []
            if isinstance(item, dict)
        )
    return len(record.get("source", "") or "")


def _chunk_stats(work: Path) -> dict:
    total = 0
    pending = 0
    pending_chars = 0
    for path in sorted(work.glob("chunk_*.json")):
        if path.name.startswith("chunks_"):
            continue
        total += 1
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            continue
        output = data.get("output")
        if not output or not (work / output).exists():
            pending += 1
            pending_chars += sum(
                _record_chars(record)
                for record in data.get("records") or []
                if isinstance(record, dict)
            )
    return {"chunks": total, "chunks_pending": pending, "pending_chars": pending_chars}


def _log_stats(work: Path) -> list[dict]:
    stats: list[dict] = []
    for path in sorted(work.glob("*.log")):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        costs = COST_RE.findall(text)
        mems = MEM_RE.findall(text)
        if not costs:
            continue
        item = {"file": path.name, "cost_s": float(costs[-1])}
        if mems:
            item["peak_mb"] = float(mems[-1])
        stats.append(item)
    return stats


def _page_count(stamp: dict) -> int | None:
    pdf = (stamp or {}).get("input")
    if not pdf or not Path(pdf).is_file():
        return None
    try:
        import fitz

        with fitz.open(pdf) as doc:
            return doc.page_count
    except Exception:
        return None


def _run_page_count(stamp: dict, pdf_pages: int | None) -> int | None:
    """Pages actually translated: the collect range, else the PDF page count."""
    spec = str((stamp or {}).get("pages") or "").strip()
    if not spec:
        return pdf_pages
    total = 0
    try:
        for part in spec.split(","):
            part = part.strip()
            if "-" in part:
                start, end = part.split("-", 1)
                total += abs(int(end) - int(start)) + 1
            elif part:
                total += 1
    except ValueError:
        return pdf_pages
    return total or pdf_pages


def collect_status(work: Path) -> dict:
    result: dict = {"work": str(work), "exists": work.is_dir()}
    if not work.is_dir():
        return result

    stamp = {}
    stamp_path = work / "run_stamp.json"
    if stamp_path.exists():
        try:
            stamp = json.loads(stamp_path.read_text(encoding="utf-8-sig"))
        except ValueError:
            stamp = {}
    result["stamp"] = {
        "pages": stamp.get("pages") or "all",
        "sha8": (stamp.get("sha256") or "")[:8],
        "lang_out": stamp.get("lang_out"),
        "input": stamp.get("input"),
    }
    pdf_pages = _page_count(stamp)
    pages = _run_page_count(stamp, pdf_pages)
    result["pages"] = pages
    result["pdf_pages"] = pdf_pages

    stats = _prompt_stats(work)
    result.update(stats)

    try:
        if not (work / "prompts.jsonl").exists():
            raise FileNotFoundError("prompts.jsonl not found - run collect first")
        pre = precheck(work)
        result.update(
            {
                "keyed": pre["keyed"],
                "via_text": pre["via_text"],
                "missing_keys": len(pre["missing"]),
                "texts_indexed": pre["texts_indexed"],
                "fallback_total": pre.get("fallback_total", 0),
                "fallback_missing": pre.get("fallback_missing", 0),
                "preflight_warnings": pre["warnings"],
            }
        )
    except (FileNotFoundError, ValueError) as exc:
        result["preflight_error"] = str(exc)

    result.update(_chunk_stats(work))
    result["logs"] = _log_stats(work)

    if pages:
        replay_s = REPLAY_FIXED_S + REPLAY_S_PER_PAGE * pages
        collect_s = COLLECT_FIXED_S + COLLECT_S_PER_PAGE * pages
        result.update(
            {
                "replay_estimate_s": round(replay_s),
                "replay_range_s": [round(replay_s * 0.75), round(replay_s * 1.25)],
                "collect_estimate_s": round(collect_s),
                "memory_estimate_mb": round(MEM_BASE_MB + MEM_MB_PER_PAGE * pages),
            }
        )
    return result


def print_report(result: dict) -> None:
    if not result.get("exists"):
        print(f"work dir not found: {result['work']}")
        return
    stamp = result.get("stamp") or {}
    pages = result.get("pages")
    print(f"work: {result['work']}")
    print(
        f"collect: pages={stamp.get('pages')}  sha={stamp.get('sha8')}  "
        f"lang={stamp.get('lang_out')}  run_pages={pages or '?'}  "
        f"pdf_pages={result.get('pdf_pages') or '?'}"
    )
    print(
        f"prompts: {result.get('prompts', 0)} "
        f"(batch {result.get('batch', 0)}, single {result.get('single', 0)})  "
        f"source_chars={result.get('source_chars', 0):,}"
    )
    if "preflight_error" in result:
        print(f"coverage: ERROR {result['preflight_error']}")
    else:
        print(
            f"coverage: keyed={result.get('keyed', 0)} "
            f"text-index={result.get('via_text', 0)} "
            f"missing={result.get('missing_keys', 0)}  "
            f"texts={result.get('texts_indexed', 0)}"
        )
        if result.get("fallback_total"):
            print(
                f"fallback: plain-path sources={result['fallback_total']} "
                f"uncovered={result.get('fallback_missing', 0)}"
            )
    if result.get("chunks"):
        print(
            f"chunks: {result['chunks']} written, "
            f"{result.get('chunks_pending', 0)} unanswered "
            f"({result.get('pending_chars', 0):,} chars)"
        )
    for log in result.get("logs", []):
        peak = f", peak {log['peak_mb']:.0f} MB" if "peak_mb" in log else ""
        print(f"log {log['file']}: {log['cost_s']:.0f}s{peak}")
    if pages:
        lo, hi = result["replay_range_s"]
        print(
            f"estimate: replay ~{_fmt_minutes(result['replay_estimate_s'])} "
            f"({_fmt_minutes(lo)}-{_fmt_minutes(hi)}), "
            f"collect ~{_fmt_minutes(result['collect_estimate_s'])}, "
            f"peak memory ~{result['memory_estimate_mb'] / 1024:.1f} GB"
        )
    else:
        print("estimate: unavailable (PDF path missing from run_stamp.json)")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--work", default=None, help="override work dir (else $ME_WORK_DIR / ./me_work)")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    args = ap.parse_args()
    work = Path(args.work).resolve() if args.work else WORK
    result = collect_status(work)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=1))
    else:
        print_report(result)
    return 0 if result.get("exists") else 2


if __name__ == "__main__":
    sys.exit(main())
