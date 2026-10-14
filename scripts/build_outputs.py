"""Build <work>/llm_outputs.json (+ fallback_outputs.json) from offline translations.

Run AFTER export_texts.py + filling zh_texts.json (or zh_slice_*.json),
BEFORE replay:

  python build_outputs.py

Inputs:
  <work>/src_texts.json     unique sources from the collect run (export step)
  <work>/zh_texts.json      object {"<source>": "<translation>"} (export creates
                            it with "" placeholders; fill values)
  <work>/zh_slice_*.json    optional parallel slices (same object format);
                            auto-merged into the above view
  <work>/glossary.json      optional object {"<source term>": "<target term>"};
                            mismatches are reported as non-fatal warnings

Outputs:
  <work>/llm_outputs.json   keyed "b:<sha>" (canonical batch payload) and
                            "s:<sha>" (single source); proactive singles are
                            added for every known source so the fallback
                            path never misses.
  <work>/fallback_outputs.json  keyed "<sha>" for the plain do_translate path.

Validation (violations are FATAL - RESULT: NEEDS_FIX):
- output != input when input > 10 tokens (gpt-4o tiktoken)
- token ratio output/input within (0.3, 3)
- <bN> style tags byte-identical (same sequence) between source and output
- paragraph count (\\n splits) equal
- every source has a non-empty translation (coverage)
- no zh key outside the source list (stale/misaligned data)
tiktoken must be importable and working; otherwise the run FAILS loudly
instead of silently skipping ratio checks.

RESULT: OK  => safe to replay.
"""

from __future__ import annotations

import json
import os
import re
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

_TAG_RE = re.compile(r"</?b\d+>")
MAX_LISTED = 20  # cap for printed missing/unknown entries


def _require_tiktoken():
    """Return a working gpt-4o encoding or fail loudly (no silent skip)."""
    try:
        import tiktoken

        enc = tiktoken.encoding_for_model("gpt-4o")
        enc.encode("probe", disallowed_special=())
        return enc
    except Exception as e:  # noqa: BLE001 - any failure is fatal here
        print("ERROR: tiktoken unavailable - refusing to validate blindly.")
        print(f"  cause: {type(e).__name__}: {e}")
        print("  fix: pip install tiktoken (first use downloads o200k_base,")
        print("       which needs network once; then it works offline)")
        sys.exit(3)


_ENC = None  # set in main()


def _tok(text: str) -> int:
    return len(_ENC.encode(text, disallowed_special=()))


def _load_sources() -> list[str]:
    p = WORK / "src_texts.json"
    if not p.exists():
        raise SystemExit(f"ERROR: {p} not found - run export_texts.py first.")
    data = json.loads(p.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise SystemExit(f"ERROR: {p} must be a JSON array of strings.")
    return [str(s) for s in data]


def _merge_obj(path: Path, into: dict[str, str], errors: list[str]) -> None:
    """Merge one {src: zh} object file; conflicting non-empty values are errors."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        errors.append(f"{path.name}: unreadable ({e})")
        return
    if not isinstance(data, dict):
        errors.append(f"{path.name}: must be a JSON object {{source: translation}}")
        return
    for k, v in data.items():
        k, v = str(k), str(v)
        if k in into:
            old = into[k]
            if old.strip() and v.strip() and old != v:
                errors.append(
                    f"conflicting translations for source in {path.name}: "
                    f"{k[:60]!r}"
                )
                continue
            if v.strip():
                into[k] = v  # filled value always wins over empty
        else:
            into[k] = v


def _load_translations(src_list: list[str]) -> dict[str, str]:
    errors: list[str] = []
    zh: dict[str, str] = {}
    zh_path = WORK / "zh_texts.json"
    if zh_path.exists():
        _merge_obj(zh_path, zh, errors)
    slices = sorted(WORK.glob("zh_slice_*.json"))
    for sp in slices:
        _merge_obj(sp, zh, errors)
    if not zh_path.exists() and not slices:
        raise SystemExit(
            "ERROR: neither zh_texts.json nor zh_slice_*.json found - "
            "run export_texts.py first, then fill translations."
        )

    src_set = set(src_list)
    unknown = [k for k in zh if k not in src_set]
    if unknown:
        errors.append(
            f"{len(unknown)} zh key(s) not present in src_texts.json "
            f"(stale or misaligned data)"
        )
        for k in unknown[:MAX_LISTED]:
            errors.append(f"  unknown key: {k[:80]!r}")
    if errors:
        print("!! errors:")
        for e in errors:
            print("   -", e)
        print("RESULT: NEEDS_FIX")
        sys.exit(1)
    return zh


def _glossary_warnings(T: dict[str, str]) -> list[str]:
    g_path = WORK / "glossary.json"
    if not g_path.exists():
        return []
    try:
        glossary = json.loads(g_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        return [f"glossary.json unreadable ({e}) - glossary checks skipped"]
    if not isinstance(glossary, dict):
        return ["glossary.json must be an object {source term: target term}"]
    warns: list[str] = []
    for src, dst in T.items():
        if not dst.strip():
            continue  # untranslated entries already reported as coverage errors
        for en_term, zh_term in glossary.items():
            if en_term.lower() in src.lower() and zh_term not in dst:
                warns.append(
                    f"term {en_term!r} -> {zh_term!r} missing in: {src[:60]!r}"
                )
    return warns


def main() -> int:
    global _ENC
    _ENC = _require_tiktoken()

    from me_translator import INPUT_MARKER, _extract_json_input, _sha

    src_list = _load_sources()
    zh = _load_translations(src_list)

    # --- coverage: every source needs a non-empty translation ---
    missing = [s for s in src_list if not zh.get(s, "").strip()]
    translated = len(src_list) - len(missing)
    print(f"coverage: {translated}/{len(src_list)} translated")
    if missing:
        print(f"!! {len(missing)} source(s) still untranslated:")
        for s in missing[:MAX_LISTED]:
            print("   -", repr(s[:100]))
        if len(missing) > MAX_LISTED:
            print(f"   ... and {len(missing) - MAX_LISTED} more")
        print("RESULT: NEEDS_FIX")
        return 1

    T = {s: zh[s] for s in src_list}

    prompts_path = WORK / "prompts.jsonl"
    if not prompts_path.exists():
        print(f"ERROR: {prompts_path} not found - run collect first.")
        return 2
    recs = [
        json.loads(line)
        for line in prompts_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]

    outputs: dict[str, str] = {}
    errors: list[str] = []

    def check(src: str, dst: str) -> None:
        if src == dst and _tok(src) > 10:
            errors.append(f"same-as-input >10tok: {src[:60]!r}")
        ti, to = _tok(src), _tok(dst)
        if ti and to and not (0.3 < to / ti < 3):
            errors.append(f"ratio {to / ti:.2f} out of range: {src[:60]!r}")
        if _TAG_RE.findall(src) != _TAG_RE.findall(dst):
            errors.append(f"tag mismatch: {src[:60]!r}")
        if src.count("\n") != dst.count("\n"):
            errors.append(f"paragraph count differs: {src[:60]!r}")

    for rec in recs:
        if rec["kind"] == "batch":
            canonical, payload = _extract_json_input(rec["prompt"])
            out_arr = []
            for item in payload:
                s = item["input"]
                if s not in T:
                    errors.append(f"batch source not translated: {s[:60]!r}")
                    out_arr.append({"id": item["id"], "output": s})
                    continue
                check(s, T[s])
                out_arr.append({"id": item["id"], "output": T[s]})
            outputs["b:" + _sha(canonical)] = json.dumps(
                out_arr, ensure_ascii=False
            )
        else:
            s = rec["source"]
            if s not in T:
                errors.append(f"single source not translated: {s[:60]!r}")
                outputs[rec["key"]] = s
            else:
                check(s, T[s])
                outputs[rec["key"]] = T[s]

    # Proactive singles for every known text (guards the replay fallback path).
    for s, d in T.items():
        outputs.setdefault("s:" + _sha(s), d)

    glossary_warns = _glossary_warnings(T)

    if errors:
        print("!! validation errors:")
        for e in errors[:50]:
            print("   -", e)
        if len(errors) > 50:
            print(f"   ... and {len(errors) - 50} more")
        print("RESULT: NEEDS_FIX")
        return 1

    (WORK / "llm_outputs.json").write_text(
        json.dumps(outputs, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    # Plain do_translate path: keyed by sha of the raw source text.
    fb = {_sha(s): d for s, d in T.items()}
    (WORK / "fallback_outputs.json").write_text(
        json.dumps(fb, ensure_ascii=False, indent=1), encoding="utf-8"
    )

    print(f"llm_outputs.json entries: {len(outputs)}")
    print(
        f"batch: {sum(1 for k in outputs if k.startswith('b:'))}  "
        f"single: {sum(1 for k in outputs if k.startswith('s:'))}  "
        f"fallback: {len(fb)}"
    )
    if glossary_warns:
        print(f"glossary warnings ({len(glossary_warns)}, non-fatal):")
        for w in glossary_warns[:MAX_LISTED]:
            print("   -", w)
    print("RESULT: OK" + (
        f" ({len(glossary_warns)} glossary warning(s))"
        if glossary_warns else ""
    ))
    return 0


if __name__ == "__main__":
    sys.exit(main())
