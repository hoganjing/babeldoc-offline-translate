"""Split <work>/src_texts.json into parallel-translation slice files.

Usage:

  python split_work.py [char_budget] [--force]

char_budget  max cumulative source chars per slice (default 4000); a single
             source longer than the budget becomes its own slice
--force      regenerate slice files even if they already exist

Reads only:
  <work>/src_texts.json     unique sources (from export_texts.py)
  <work>/zh_texts.json      existing translations (best effort)
  <work>/zh_slice_*.json    pre-existing slices (best effort, for seeding)

Writes <work>/zh_slice_01.json, zh_slice_02.json, ... (same object format;
build_outputs.py auto-merges all slices). Each source keeps its existing
translation when one is known, otherwise "". Never modifies src_texts.json
or zh_texts.json. Exit 0; exit 2 on missing/unreadable src_texts.json or
bad arguments.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

WORK = Path(os.environ.get("ME_WORK_DIR") or (Path.cwd() / "me_work")).resolve()

USAGE = "usage: python split_work.py [char_budget] [--force]"
DEFAULT_BUDGET = 4000


def load_sources(path: Path) -> list[str] | None:
    """Load src_texts.json as a list of strings; None + message on error."""
    if not path.exists():
        print(f"ERROR: {path} not found - run export_texts.py first")
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(f"ERROR: cannot read {path}: {exc}")
        return None
    if not isinstance(data, list) or not all(isinstance(s, str) for s in data):
        print(f"ERROR: {path} must be a JSON array of strings")
        return None
    return data


def load_seed(existing_slices: list[Path]) -> dict[str, str]:
    """Known translations from zh_texts.json + pre-existing slices."""
    seed: dict[str, str] = {}
    for path in [WORK / "zh_texts.json", *existing_slices]:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        for key, value in data.items():
            if isinstance(key, str) and isinstance(value, str):
                if value and not seed.get(key):
                    seed[key] = value
    return seed


def pack_slices(srcs: list[str], budget: int) -> list[list[str]]:
    """Greedy first-fit packing preserving source order."""
    slices: list[list[str]] = []
    current: list[str] = []
    current_len = 0
    for src in srcs:
        if current and current_len + len(src) > budget:
            slices.append(current)
            current = []
            current_len = 0
        current.append(src)
        current_len += len(src)
    if current:
        slices.append(current)
    return slices


def slice_name(index: int) -> str:
    """Zero-padded slice filename for 1-based index."""
    return f"zh_slice_{index:02d}.json"


def main() -> int:
    force = False
    positional: list[str] = []
    for arg in sys.argv[1:]:
        if arg == "--force":
            force = True
        elif arg.startswith("-"):
            print(f"ERROR: unknown option {arg}")
            print(USAGE)
            return 2
        else:
            positional.append(arg)
    if len(positional) > 1:
        print(USAGE)
        return 2
    budget = DEFAULT_BUDGET
    if positional:
        try:
            budget = int(positional[0])
        except ValueError:
            print(USAGE)
            return 2
        if budget < 1:
            print(f"ERROR: char_budget must be >= 1, got {budget}")
            print(USAGE)
            return 2

    srcs = load_sources(WORK / "src_texts.json")
    if srcs is None:
        return 2

    existing = sorted(WORK.glob("zh_slice_*.json"))
    if existing and not force:
        print(
            f"slices already exist ({len(existing)}) - rerun with --force"
            " to regenerate (existing translations are preserved either way)"
        )
        for path in existing:
            print(path.name)
        return 0

    seed = load_seed(existing)
    slices = pack_slices(srcs, budget)

    for path in existing:  # drop stale slices before rewriting
        try:
            path.unlink()
        except OSError as exc:
            print(f"ERROR: cannot remove stale slice {path}: {exc}")
            return 2

    total_texts = 0
    total_chars = 0
    for index, chunk in enumerate(slices, start=1):
        obj = {src: seed.get(src, "") for src in chunk}
        name = slice_name(index)
        (WORK / name).write_text(
            json.dumps(obj, ensure_ascii=False, indent=1), encoding="utf-8"
        )
        chars = sum(len(src) for src in chunk)
        total_texts += len(chunk)
        total_chars += chars
        print(f"{name}: {len(chunk)} texts, {chars} chars")
    print(f"slices: {len(slices)}  texts: {total_texts}  chars: {total_chars}")
    print(
        "fill every value (non-empty), then run build_outputs.py - it"
        " auto-merges slices and reports coverage"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
