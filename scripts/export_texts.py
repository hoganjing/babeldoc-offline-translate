"""Export unique source texts from a collect run and sync the translation file.

Run AFTER collect, BEFORE translation:

  python export_texts.py

Reads  <work>/prompts.jsonl (batch + single records written by MeTranslator)
Writes <work>/src_texts.json  -- JSON array of unique source strings, in
        first-appearance order (the canonical source list).
Ensures <work>/zh_texts.json  -- JSON object {"<source>": "<translation>"}.
        Creates it if missing; adds "" placeholders for new sources; keeps
        existing translations; prunes keys that are no longer sources
        (announced). Fill values (or use split_work.py for parallel slices),
        then run build_outputs.py.

Prints coverage so the agent knows how much translation remains.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

WORK = Path(
    os.environ.get("ME_WORK_DIR") or (Path.cwd() / "me_work")
).resolve()


def main() -> int:
    from me_translator import INPUT_MARKER, _extract_json_input

    prompts_path = WORK / "prompts.jsonl"
    if not prompts_path.exists():
        print(f"ERROR: {prompts_path} not found - run collect first.")
        return 2

    recs = [
        json.loads(line)
        for line in prompts_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]

    texts: dict[str, None] = {}  # insertion-ordered set
    n_batch = n_single = 0
    for rec in recs:
        if rec["kind"] == "batch":
            n_batch += 1
            _, payload = _extract_json_input(rec["prompt"])
            for item in payload:
                texts.setdefault(item["input"], None)
        else:
            n_single += 1
            texts.setdefault(rec["source"], None)

    unparsed = WORK / "unparsed.jsonl"
    if unparsed.exists() and unparsed.stat().st_size > 0:
        print(f"WARNING: {unparsed} is non-empty - some prompts were not parsed.")
        print("         Translate those too (see SKILL.md 'Unparsed prompts').")

    src_list = list(texts)
    src_path = WORK / "src_texts.json"
    src_path.write_text(
        json.dumps(src_list, ensure_ascii=False, indent=1), encoding="utf-8"
    )

    # --- sync zh_texts.json object (source-keyed, no index alignment) ---
    zh_path = WORK / "zh_texts.json"
    zh: dict[str, str] = {}
    if zh_path.exists():
        try:
            loaded = json.loads(zh_path.read_text(encoding="utf-8"))
            if not isinstance(loaded, dict):
                raise ValueError("not a JSON object")
            zh = {str(k): str(v) for k, v in loaded.items()}
        except (ValueError, json.JSONDecodeError) as e:
            print(f"ERROR: {zh_path} is not a valid object: {e}")
            return 2
    src_set = set(src_list)
    stale = [k for k in zh if k not in src_set]
    if stale:
        for k in stale:
            zh.pop(k)
        print(f"NOTE: pruned {len(stale)} stale key(s) from zh_texts.json "
              f"(no longer in the source list)")
    added = 0
    for s in src_list:
        if s not in zh:
            zh[s] = ""
            added += 1
    zh_path.write_text(
        json.dumps(zh, ensure_ascii=False, indent=1), encoding="utf-8"
    )

    translated = sum(1 for v in zh.values() if v.strip())
    remaining = len(src_list) - translated
    total_chars = sum(len(t) for t in src_list)
    print(f"batch prompts: {n_batch}  single prompts: {n_single}")
    print(f"unique texts : {len(src_list)}  total chars: {total_chars}")
    print(f"src written  : {src_path}")
    print(f"zh synced    : {zh_path} (+{added} new placeholder(s))")
    print(f"coverage     : {translated}/{len(src_list)} translated, {remaining} remaining")
    if remaining:
        print("next: fill zh_texts.json values"
              " (or run split_work.py for parallel slices), then build_outputs.py")
    else:
        print("next: run build_outputs.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
