#!/usr/bin/env python3
"""Pre-merge validation gate for pure-pipe translation chunks.

Checks every ``chunk_*.json`` (and ``chunk_fix_*`` / ``chunk_retry_*``
variants) against its ``llm_chunk_*.json`` counterpart:

  * every prompt key in the chunk is present in the llm output;
  * batch values are lists with the same length and id set as the source items;
  * the ``<bN>`` tag sequence is byte-identical between source and translation
    (the single most important invariant — tag misalignment corrupts formulas).

This is the gate that was previously hand-rolled per book as ``_validate.py``.
Run it BEFORE ``pipe_merge.py``. Exit 0 only when every translated chunk is
present and clean; exit 3 on any missing chunk or broken record, so a non-zero
exit means "do not merge yet".

Usage:
  python pipe_validate.py [--work DIR]
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

WORK = Path(
    os.environ.get("ME_WORK_DIR") or (Path.cwd() / "me_work")
).resolve()
TAG = re.compile(r"</?b\d+>")


def llm_name(chunk_path: Path) -> Path:
    """Map chunk_*.json -> llm_chunk_*.json (handles fix/retry variants too)."""
    return chunk_path.with_name(chunk_path.name.replace("chunk_", "llm_chunk_", 1))


def validate_chunk(chunk_path: Path) -> list[str]:
    problems: list[str] = []
    try:
        data = json.loads(chunk_path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        return [f"cannot read chunk: {exc}"]
    records = data.get("records") or []
    llm_path = llm_name(chunk_path)
    if not llm_path.exists():
        return [f"missing output file {llm_path.name}"]
    try:
        out = json.loads(llm_path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        return [f"cannot read {llm_path.name}: {exc}"]
    if not isinstance(out, dict):
        return [f"{llm_path.name}: top-level must be a JSON object"]

    for r in records:
        if not isinstance(r, dict):
            continue
        k = r.get("key")
        if k is None:
            continue
        if k not in out:
            problems.append(f"{chunk_path.name}: MISSING key {k}")
            continue
        v = out[k]
        if r.get("kind") == "batch":
            items = r.get("items") or []
            if not isinstance(v, list) or len(v) != len(items):
                got = len(v) if isinstance(v, list) else "?"
                problems.append(
                    f"{chunk_path.name}: {k} length {got} != {len(items)}"
                )
                continue
            by_id = {
                x.get("id"): (x.get("output") or "")
                for x in v
                if isinstance(x, dict)
            }
            for it in items:
                if not isinstance(it, dict):
                    continue
                iid = it.get("id")
                src = it.get("input", "")
                tr = by_id.get(iid)
                if tr is None:
                    problems.append(f"{chunk_path.name}: {k}#{iid} id missing")
                    continue
                if TAG.findall(src) != TAG.findall(tr):
                    problems.append(f"{chunk_path.name}: {k}#{iid} TAG mismatch")
        else:
            if not isinstance(v, str):
                problems.append(f"{chunk_path.name}: {k} not a string")
                continue
            if TAG.findall(r.get("source", "")) != TAG.findall(v):
                problems.append(f"{chunk_path.name}: {k} TAG mismatch")
    return problems


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--work", default=None, help="override work dir (else $ME_WORK_DIR / ./me_work)")
    args = ap.parse_args()
    work = Path(args.work).resolve() if args.work else WORK
    if not work.is_dir():
        print(f"work dir not found: {work}")
        return 2

    chunks = sorted(
        p for p in work.glob("chunk_*.json") if not p.name.startswith("chunks_")
    )
    total = len(chunks)
    all_problems: list[str] = []
    done = 0
    for c in chunks:
        probs = validate_chunk(c)
        if probs:
            all_problems.extend(probs)
        else:
            done += 1

    print(
        f"[me] pipe_validate: {total} chunk file(s), {done} clean, "
        f"{len(all_problems)} problem(s)"
    )
    if all_problems:
        for p in all_problems[:50]:
            print(f"       - {p}")
        extra = len(all_problems) - 50
        if extra > 0:
            print(f"       ... and {extra} more")
        return 3
    print("[me] OK: all chunks present and clean — safe to pipe_merge")
    return 0


if __name__ == "__main__":
    sys.exit(main())
