#!/usr/bin/env python3
"""Predict replay coverage BEFORE spending a full rendering pass.

Replay can take tens of minutes on a book-sized PDF. A missing translation is
only discovered at the end (``missing_llm=...``), after the layout model, the
translation walk and both PDF writes. This module answers the same question up
front, from the files merge already wrote:

  prompts.jsonl     what the engine asked during collect
  llm_outputs.json  what replay will serve by key
  text_index.json   exact-source fallback used when a key does not exist

A prompt counts as covered when
  * its key has a complete value in ``llm_outputs.json`` (batch: every id, no
    empty output), or
  * every source text it contains exists in ``text_index.json`` (byte-identical,
    tags included) and will be served by ``MeTranslator``'s runtime fallback.

Keys that replay generates on the fly (not present in prompts.jsonl) cannot be
predicted here; the runtime text fallback and ``missing.jsonl`` still cover
them. Missing prompts found here are a hard lower bound.

Usage:
  python replay_precheck.py [--work DIR]

Exit codes: 0 all prompts covered; 3 gaps; 2 prompts.jsonl missing.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from pipe_merge import _extract_json_input, _parse_batch_output_map, _sha

WORK = Path(
    os.environ.get("ME_WORK_DIR") or (Path.cwd() / "me_work")
).resolve()

SINGLE_MARKER = "Now translate the following text:"


def _load_map(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except ValueError as exc:
        raise ValueError(f"cannot parse {path.name}: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"{path.name} must contain a JSON object")
    return data


def _load_prompts(path: Path) -> list[dict]:
    records: list[dict] = []
    with path.open(encoding="utf-8-sig") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if isinstance(record, dict):
                records.append(record)
    return records


def _single_source(prompt: str) -> str:
    """Extract the source paragraph from a single-paragraph prompt.

    Uses the FIRST marker occurrence, exactly like
    ``me_translator._extract_single_source``: the template puts the source
    after the first marker, so a source that itself contains the marker string
    must not be truncated at the later occurrence.
    """
    pos = prompt.find(SINGLE_MARKER)
    if pos == -1:
        return ""
    return prompt[pos + len(SINGLE_MARKER):].lstrip("\n")


def _snippet(text: str, limit: int = 90) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 3] + "..."


def precheck(work: Path) -> dict:
    """Return a coverage prediction for a replay in ``work``."""
    prompts_path = Path(work) / "prompts.jsonl"
    if not prompts_path.exists():
        raise FileNotFoundError(f"prompts.jsonl not found in {work}")

    prompts = _load_prompts(prompts_path)
    outputs = _load_map(Path(work) / "llm_outputs.json")
    raw_index = _load_map(Path(work) / "text_index.json")
    index = {
        key: value
        for key, value in raw_index.items()
        if isinstance(value, str) and value.strip()
    }

    keyed = 0
    via_text = 0
    missing: list[dict] = []

    for record in prompts:
        key = record.get("key") or ""
        kind = record.get("kind") or ("batch" if key.startswith("b:") else "single")
        prompt = record.get("prompt") or ""

        if kind == "batch":
            try:
                items = _extract_json_input(prompt)
            except ValueError:
                missing.append(
                    {
                        "key": key,
                        "kind": "batch",
                        "reason": "prompt payload not parseable",
                        "sources": [],
                    }
                )
                continue
            ids = []
            for item in items:
                if isinstance(item, dict) and "id" in item:
                    try:
                        ids.append(int(item["id"]))
                    except (TypeError, ValueError):
                        pass
            sources = [
                item.get("input", "")
                for item in items
                if isinstance(item, dict)
            ]

            value = outputs.get(key)
            mapping = _parse_batch_output_map(value) if value is not None else None
            usable = (
                mapping is not None
                and bool(ids)
                and all(
                    ident in mapping and isinstance(mapping[ident], str) and mapping[ident].strip()
                    for ident in ids
                )
            )
            if usable:
                keyed += 1
                continue
            if sources and all(_sha(source) in index for source in sources):
                via_text += 1
                continue

            absent = [s for s in sources if _sha(s) not in index]
            reason = (
                "key missing or incomplete in llm_outputs.json; "
                f"{len(absent)}/{len(sources)} source(s) not in text_index.json"
            )
            missing.append(
                {
                    "key": key,
                    "kind": "batch",
                    "reason": reason,
                    "sources": [_snippet(s) for s in absent[:2]],
                    "ids": len(ids),
                }
            )
            continue

        # single / plain prompt: s:<sha256(source)>
        # Older prompts.jsonl records carry "source" instead of "prompt".
        source = _single_source(prompt) if prompt else (record.get("source") or "")
        value = outputs.get(key)
        if isinstance(value, str) and value.strip():
            keyed += 1
            continue
        digest = key.split(":", 1)[1] if ":" in key else _sha(source)
        if digest in index:
            via_text += 1
            continue
        missing.append(
            {
                "key": key,
                "kind": "single",
                "reason": "key missing in llm_outputs.json and source not in text_index.json",
                "sources": [_snippet(source)],
            }
        )

    warnings: list[str] = []
    outputs_path = Path(work) / "llm_outputs.json"
    index_path = Path(work) / "text_index.json"

    # The plain do_translate path is addressed by sha256(text), not by prompt
    # key. Sources that took that path during collect are listed in
    # fallback_src.jsonl; replay may take it again for the same paragraph, so
    # every one needs a translation in fallback_outputs.json (or, at runtime, in
    # text_index.json). Otherwise that paragraph is emitted in the source
    # language at the very end of the run.
    fallback_total = 0
    fallback_missing: list[dict] = []
    fb_src_path = Path(work) / "fallback_src.jsonl"
    fb_outputs_path = Path(work) / "fallback_outputs.json"
    if fb_src_path.exists():
        fb_outputs = _load_map(fb_outputs_path)
        seen: set[str] = set()
        for record in _load_prompts(fb_src_path):
            source = record.get("src") or ""
            if not source.strip():
                continue
            digest = record.get("key") or _sha(source)
            if digest in seen:
                continue
            seen.add(digest)
            fallback_total += 1
            if digest in fb_outputs or digest in index:
                continue
            item = {
                "key": digest,
                "kind": "fallback",
                "reason": "plain-path source has no translation in "
                "fallback_outputs.json or text_index.json",
                "sources": [_snippet(source)],
            }
            fallback_missing.append(item)
            missing.append(item)
        if fallback_total and not fb_outputs_path.exists():
            warnings.append(
                "fallback_outputs.json is missing - run pipe_merge.py to "
                "backfill the plain-path translations"
            )

    if outputs_path.exists() and index_path.exists():
        try:
            if outputs_path.stat().st_mtime - index_path.stat().st_mtime > 2:
                warnings.append(
                    "text_index.json is older than llm_outputs.json - "
                    "run pipe_merge.py to refresh it"
                )
        except OSError:
            pass
    if not index_path.exists() and len(prompts) > len(outputs):
        warnings.append(
            "text_index.json is missing - runtime exact-text fallback is off"
        )

    return {
        "prompts": len(prompts),
        "keyed": keyed,
        "via_text": via_text,
        "missing": missing,
        "texts_indexed": len(index),
        "fallback_total": fallback_total,
        "fallback_missing": len(fallback_missing),
        "warnings": warnings,
    }


def format_report(result: dict, limit: int = 8) -> str:
    lines = [
        "[me] preflight: "
        f"prompts={result['prompts']} keyed={result['keyed']} "
        f"text-index={result['via_text']} missing={len(result['missing'])} "
        f"(texts indexed={result['texts_indexed']})"
    ]
    if result.get("fallback_total"):
        lines.append(
            "[me] preflight: "
            f"plain-path sources={result['fallback_total']} "
            f"uncovered={result.get('fallback_missing', 0)}"
        )
    for warning in result.get("warnings", []):
        lines.append(f"[me] WARNING: {warning}")
    for item in result["missing"][:limit]:
        head = f"       - {item['key']}"
        if item.get("ids"):
            head += f" ({item['ids']} ids)"
        lines.append(f"{head}: {item['reason']}")
        for source in item.get("sources", []):
            lines.append(f"         {source}")
    extra = len(result["missing"]) - limit
    if extra > 0:
        lines.append(f"       ... and {extra} more")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--work", default=None, help="override work dir (else $ME_WORK_DIR / ./me_work)")
    args = ap.parse_args()
    work = Path(args.work).resolve() if args.work else WORK
    try:
        result = precheck(work)
    except FileNotFoundError as exc:
        print(f"[me] ERROR: {exc}")
        return 2
    except ValueError as exc:
        print(f"[me] ERROR: {exc}")
        return 2
    print(format_report(result))
    return 3 if result["missing"] else 0


if __name__ == "__main__":
    sys.exit(main())
