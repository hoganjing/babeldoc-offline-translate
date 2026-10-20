#!/usr/bin/env python3
"""Merge llm_chunk_*.json into llm_outputs.json and validate <bN> integrity.

Pure-pipe flow (v2.5). Also reuses single-prompt translations to seed
fallback_outputs.json, which covers babeldoc's plain do_translate path when it
fires during replay.

Usage:
  python pipe_merge.py [--work DIR]
"""
from __future__ import annotations

import argparse
import hashlib
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

INPUT_MARKER = "## Here is the input:"
TAG_RE = re.compile(r"</?b\d+>")


def _extract_json_input(prompt: str) -> list:
    """Parse the batch payload array after the input marker (mirrors me_translator)."""
    decoder = json.JSONDecoder()
    positions: list[int] = []
    pos = prompt.find(INPUT_MARKER)
    while pos != -1:
        positions.append(pos)
        pos = prompt.find(INPUT_MARKER, pos + 1)
    for p in reversed(positions):
        rest = prompt[p + len(INPUT_MARKER) :].lstrip()
        try:
            obj, _ = decoder.raw_decode(rest)
        except ValueError:
            continue
        if isinstance(obj, list):
            return obj
    raise ValueError("no parseable json input array found in prompt")


def _tag_seq(s) -> list:
    return TAG_RE.findall(s or "")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--work", default=None, help="override work dir (else $ME_WORK_DIR / ./me_work)")
    args = ap.parse_args()
    work = Path(args.work).resolve() if args.work else WORK

    prompts = work / "prompts.jsonl"
    if not prompts.exists():
        print(f"ERROR: {prompts} not found - run collect first.")
        return 2
    recs = [
        json.loads(l)
        for l in prompts.read_text(encoding="utf-8").splitlines()
        if l.strip()
    ]
    kind_by_key = {r["key"]: r["kind"] for r in recs}

    chunk_files = sorted(work.glob("llm_chunk_*.json"))
    if not chunk_files:
        print("ERROR: no llm_chunk_*.json found - run subagents first.")
        return 2

    merged: dict[str, object] = {}
    for p in chunk_files:
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            print(f"!! unreadable {p.name}: {e}")
            continue
        if not isinstance(data, dict):
            print(f"!! {p.name}: expected object {{key: value}}")
            continue
        for k, v in data.items():
            merged[k] = v
    print(f"merged keys: {len(merged)} (expected {len(recs)})")

    missing = [k for k in kind_by_key if k not in merged]
    if missing:
        print(f"MISSING KEYS: {len(missing)} -> {missing[:5]}")

    # Normalize batch values to JSON strings (replay expects a JSON string of
    # [{"id", "output"}]; a list is accepted here and serialized).
    for k, v in list(merged.items()):
        if kind_by_key.get(k) == "batch":
            if isinstance(v, list):
                merged[k] = json.dumps(v, ensure_ascii=False)
            elif isinstance(v, str):
                try:
                    json.loads(v)
                except Exception as e:
                    print(f"BAD BATCH JSON for {k}: {e}")
            else:
                print(f"!! batch {k}: unexpected value type {type(v).__name__}")

    # Validate <bN> tag sequences: input tag list must equal output tag list.
    problems: list[tuple[str, str]] = []
    for r in recs:
        key = r["key"]
        if key not in merged:
            continue
        if r["kind"] == "batch":
            parsed = _extract_json_input(r["prompt"])
            out_val = merged[key]
            try:
                out_list = json.loads(out_val) if isinstance(out_val, str) else out_val
            except Exception:
                problems.append((key, "output not valid JSON array"))
                continue
            out_by_id = {it["id"]: it.get("output", "") for it in out_list}
            for it in parsed:
                inp = it.get("input", "")
                o = out_by_id.get(it["id"], "")
                if _tag_seq(inp) != _tag_seq(o):
                    problems.append(
                        (f"{key}#id{it['id']}", f"in={_tag_seq(inp)} out={_tag_seq(o)}")
                    )
        else:
            src = r["source"]
            o = merged[key]
            if isinstance(o, list):
                o = o[0] if o else ""
            if _tag_seq(src) != _tag_seq(o):
                problems.append((key, f"in={_tag_seq(src)} out={_tag_seq(o)}"))

    print(f"\nTAG-SEQUENCE PROBLEMS: {len(problems)}")
    for k, d in problems[:40]:
        print(f"  {k}: {d}")

    (work / "llm_outputs.json").write_text(
        json.dumps(merged, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    print(f"\nwrote llm_outputs.json ({len(merged)} keys)")

    # Reuse single translations to cover the plain do_translate fallback path.
    fb_src = work / "fallback_src.jsonl"
    if fb_src.exists():
        single_by_sha: dict[str, object] = {
            k[2:]: v for k, v in merged.items() if k.startswith("s:")
        }
        fb: dict[str, object] = {}
        for line in fb_src.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            rec = json.loads(line)
            h = rec.get("key") or hashlib.sha256(
                rec.get("src", "").encode("utf-8")
            ).hexdigest()
            if h in single_by_sha:
                fb[h] = single_by_sha[h]
        (work / "fallback_outputs.json").write_text(
            json.dumps(fb, ensure_ascii=False, indent=1), encoding="utf-8"
        )
        print(
            f"wrote fallback_outputs.json ({len(fb)} keys from "
            f"{len(single_by_sha)} single translations)"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
