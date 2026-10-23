#!/usr/bin/env python3
"""Merge llm_chunk_*.json into llm_outputs.json, validate, and close coverage gaps.

Pure-pipe flow (v2.6). Also reuses translations to seed fallback_outputs.json,
which covers babeldoc's plain do_translate path when it fires during replay.

Why a text index
----------------
BabelDOC addresses translations by prompt key:

    b:<sha256(canonical batch json)>   batch of paragraphs
    s:<sha256(source text)>            one paragraph

Keys are not stable between runs: the same paragraph can be requested as part of
a differently-composed batch (new ``b:`` key) or as a single prompt (``s:`` key)
even though its source text is unchanged. Treating every key as a separate unit
therefore reports huge fake gaps (duplicates) while still missing real ones.

The invariant that actually matters is the SOURCE TEXT. Merge builds
``text_index`` = {source text -> translation} from every translated record and
uses it to fill keys no agent wrote. Matching is **exact byte-for-byte**: a
translation is only reused when the source text (including every ``<bN>`` tag)
is identical, so tags can never be re-mapped or misnumbered. Anything that does
not match exactly is reported as a real coverage gap instead of being guessed.

Usage:
  python pipe_merge.py [--work DIR] [--allow-gaps] [--strict] [--retry-content]
                       [--conflict POLICY]

Exit codes: 0 complete; 2 missing inputs; 3 incomplete (gaps / tag /
structural problems / retry values whose tags are invalid) unless --allow-gaps
downgrades gaps to a warning. Content warnings (untranslated / too short / too
long) are advisory unless --strict is given.

When anything is still defective, `retry.jsonl` lists those segments with their
source text. Translate it with `pipe_prep.py --from-retry retry.jsonl`, write
`llm_chunk_retry_*.json`, and merge again: text-addressed retry values override
the previous bad value, including a single item inside a batch key.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from collections import Counter, OrderedDict
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

WORK = Path(
    os.environ.get("ME_WORK_DIR") or (Path.cwd() / "me_work")
).resolve()

INPUT_MARKER = "## Here is the input:"
TAG_RE = re.compile(r"</?b\d+>")

# Scripts a translated segment must contain for CJK targets.
TARGET_SCRIPT = {
    "zh": re.compile(r"[\u4e00-\u9fff]"),
    "zh-cn": re.compile(r"[\u4e00-\u9fff]"),
    "zh-tw": re.compile(r"[\u4e00-\u9fff]"),
    "ja": re.compile(r"[\u4e00-\u9fff]"),
    "ko": re.compile(r"[\uac00-\ud7af]"),
}
# Bibliography entries and citations legitimately stay untranslated.
REFERENCE_RE = re.compile(
    r"(?:\b(?:19|20)\d{2}\b|\bet al\.?\b|\bdoi\s*:|\bISBN\b|\bpp?\.\s*\d)",
    re.IGNORECASE,
)


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


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


def _as_batch_json(value) -> str | None:
    """Normalize a batch value to the JSON string replay expects."""
    if isinstance(value, list):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, str):
        return value
    return None


def _parse_batch_output(value):
    """Return [(id, output)] or None when the value is not a usable array."""
    raw = _as_batch_json(value)
    if raw is None:
        return None
    try:
        parsed = json.loads(raw)
    except Exception:
        return None
    if not isinstance(parsed, list):
        return None
    out = []
    for item in parsed:
        if not isinstance(item, dict) or "id" not in item:
            return None
        try:
            ident = int(item["id"])
        except (TypeError, ValueError):
            return None
        output = item.get("output", "")
        if not isinstance(output, str):
            return None
        out.append((ident, output))
    return out


def _parse_batch_output_map(value) -> dict | None:
    pairs = _parse_batch_output(value)
    if pairs is None:
        return None
    mapping: dict[int, str] = {}
    for ident, output in pairs:
        mapping[ident] = output
    return mapping


def _word_count(text: str) -> int:
    return len(re.findall(r"[A-Za-z0-9]+", text))


def _content_flags(source: str, output: str, lang_out: str) -> list[str]:
    """Content-level problems that id/tag validation cannot see."""
    src = source.strip()
    out = output.strip()
    if not src or not out:
        return []
    # Only judge prose. Formula fragments, author lists, publisher addresses and
    # single-word labels are legitimately identical in both languages; treating
    # them as "untranslated" drowns the real hits in noise.
    plain = TAG_RE.sub(" ", src)
    if len(re.findall(r"\b[a-z]{3,}\b", plain)) < 6:
        return []
    flags: list[str] = []
    reference = bool(REFERENCE_RE.search(src))
    if src == out and _word_count(plain) > 10 and not reference:
        flags.append("identical")
    script = TARGET_SCRIPT.get((lang_out or "").strip().lower())
    if script and len(src) >= 200 and not script.search(out) and not reference:
        flags.append("no_target_script")
    if len(src) >= 200 and not reference:
        if len(out) < 0.10 * len(src):
            flags.append("too_short")
        elif len(out) > 4.0 * len(src):
            flags.append("too_long")
    # An identical output already explains the missing target script; one clear
    # diagnosis per segment keeps the report actionable.
    if "identical" in flags and "no_target_script" in flags:
        flags.remove("no_target_script")
    return flags


def _target_lang(work: Path) -> str:
    stamp = work / "run_stamp.json"
    if stamp.exists():
        try:
            data = json.loads(stamp.read_text(encoding="utf-8-sig"))
            if isinstance(data, dict) and data.get("lang_out"):
                return str(data["lang_out"])
        except (OSError, ValueError):
            pass
    return os.environ.get("ME_LANG_OUT", "zh")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--work", default=None, help="override work dir (else $ME_WORK_DIR / ./me_work)")
    ap.add_argument(
        "--allow-gaps",
        action="store_true",
        help="exit 0 even when coverage gaps remain (not recommended)",
    )
    ap.add_argument(
        "--conflict",
        choices=("error", "first", "last"),
        default="error",
        help="policy when two chunk files translate the same key differently "
        "(default: error; 'last' keeps the newest file by mtime)",
    )
    ap.add_argument(
        "--strict",
        action="store_true",
        help="treat content warnings (untranslated / too short / too long) as fatal",
    )
    ap.add_argument(
        "--retry-content",
        action="store_true",
        help="include content warnings in retry.jsonl (off by default: advisory)",
    )
    ap.add_argument(
        "--import-missing",
        action="store_true",
        help="also fold replay's missing.jsonl / missing_fallback.jsonl into retry.jsonl",
    )
    ap.add_argument(
        "--glossary",
        default=None,
        help="glossary JSON {source: target} for adherence warnings; default "
        "<work>/glossary.json if present",
    )
    ap.add_argument(
        "--no-glossary",
        action="store_true",
        help="skip glossary adherence warnings (including <work>/glossary.json)",
    )
    args = ap.parse_args()
    work = Path(args.work).resolve() if args.work else WORK
    lang_out = _target_lang(work)

    glossary: list[tuple[str, str, re.Pattern]] = []
    if not args.no_glossary:
        gpath = Path(args.glossary) if args.glossary else work / "glossary.json"
        if not gpath.is_absolute():
            gpath = work / gpath
        if gpath.exists():
            try:
                from pipe_prep import load_glossary

                glossary = load_glossary(gpath)
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                if args.glossary:
                    print(f"ERROR: glossary unreadable ({exc})")
                    return 2
                print(f"WARNING: glossary unreadable ({exc}) - skipping adherence check")
            else:
                print(f"glossary: {len(glossary)} terms from {gpath.name}")
        elif args.glossary:
            print(f"ERROR: glossary not found: {gpath}")
            return 2

    prompts = work / "prompts.jsonl"
    if not prompts.exists():
        print(f"ERROR: {prompts} not found - run collect first.")
        return 2
    recs = [
        json.loads(l)
        for l in prompts.read_text(encoding="utf-8-sig").splitlines()
        if l.strip()
    ]
    kind_by_key = {r["key"]: r["kind"] for r in recs}

    # sha256(source) -> source, so a text-addressed retry value ("t:<sha>") can
    # be resolved back to the text it belongs to.
    sha_to_source: dict[str, str] = {}
    key_text_shas: dict[str, set[str]] = {}
    for r in recs:
        if r["kind"] == "batch":
            try:
                inputs = _extract_json_input(r["prompt"])
            except ValueError:
                continue
            digests: set[str] = set()
            for it in inputs:
                text = it.get("input", "")
                if text.strip():
                    digest = _sha(text)
                    sha_to_source[digest] = text
                    digests.add(digest)
            key_text_shas[r["key"]] = digests
        elif r["source"].strip():
            digest = _sha(r["source"])
            sha_to_source[digest] = r["source"]
            key_text_shas[r["key"]] = {digest}

    chunk_files = sorted(work.glob("llm_chunk_*.json"))
    if not chunk_files:
        print("ERROR: no llm_chunk_*.json found - run subagents first.")
        return 2

    merged: dict[str, object] = {}
    owner: dict[str, str] = {}
    owner_mtime: dict[str, float] = {}
    source_mtime: dict[str, int] = {}
    text_key_mtime: dict[str, int] = {}
    unreadable: list[str] = []
    conflicts: list[str] = []
    conflict_files: dict[str, list[str]] = {}
    extra_keys: list[str] = []

    for p in chunk_files:
        try:
            data = json.loads(p.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError) as e:
            unreadable.append(f"{p.name}: {e}")
            continue
        if not isinstance(data, dict):
            unreadable.append(f"{p.name}: expected object {{key: value}}")
            continue
        mtime = p.stat().st_mtime
        mtime_ns = p.stat().st_mtime_ns
        is_text_fix = p.name.startswith(("llm_chunk_retry_", "llm_chunk_fix_"))
        if not is_text_fix:
            for k in data:
                for digest in key_text_shas.get(k, ()):
                    if mtime_ns > source_mtime.get(digest, -1):
                        source_mtime[digest] = mtime_ns
        for k, v in data.items():
            if kind_by_key.get(k) == "batch" or k.startswith("b:"):
                v = _as_batch_json(v)
                if v is None:
                    unreadable.append(f"{p.name}:{k}: unexpected batch value type")
                    continue
            if k.startswith("t:"):
                if isinstance(v, list):
                    v = v[0] if v else ""
                if not isinstance(v, str):
                    unreadable.append(f"{p.name}:{k}: text fix value must be a string")
                    continue
                digest = k[2:]
                if mtime_ns > text_key_mtime.get(digest, -1):
                    text_key_mtime[digest] = mtime_ns
            if k in merged:
                if merged[k] != v:
                    if k not in conflict_files:
                        conflicts.append(k)
                        conflict_files[k] = [owner.get(k, "?")]
                    if p.name not in conflict_files[k]:
                        conflict_files[k].append(p.name)
                    if args.conflict == "last" and mtime >= owner_mtime.get(k, 0.0):
                        merged[k] = v
                        owner[k] = p.name
                        owner_mtime[k] = mtime
                continue
            merged[k] = v
            owner[k] = p.name
            owner_mtime[k] = mtime
            if k not in kind_by_key and not k.startswith("t:"):
                extra_keys.append(k)

    # ---- text index: source text -> translation -------------------------
    index: dict[str, str] = {}
    index_bad_tags: list[str] = []
    inconsistent: list[str] = []

    def add_text(source: str, output: str) -> None:
        if not source.strip() or not output.strip():
            return
        if _tag_seq(source) != _tag_seq(output):
            if source not in index_bad_tags:
                index_bad_tags.append(source)
            return
        prev = index.get(source)
        if prev is None:
            index[source] = output
        elif prev != output:
            if source not in inconsistent:
                inconsistent.append(source)

    for r in recs:
        key = r["key"]
        if key not in merged:
            continue
        if r["kind"] == "batch":
            try:
                inputs = _extract_json_input(r["prompt"])
            except ValueError:
                continue
            outs = _parse_batch_output_map(merged[key])
            if outs is None:
                continue
            for item in inputs:
                ident = item.get("id")
                if ident in outs:
                    add_text(item.get("input", ""), outs[ident])
        else:
            value = merged[key]
            if isinstance(value, list):
                value = value[0] if value else ""
            if isinstance(value, str):
                add_text(r["source"], value)

    # Text-addressed fix-ups: llm_chunk_fix_*.json returns {"t:<sha>": "<zh>"}
    # for records produced by `pipe_prep.py --from-missing`. The source text is
    # recovered from coverage_missing.jsonl so these enter the same index.
    # Only sources declared by the active round may contribute a t: value.
    coverage_missing = work / "coverage_missing.jsonl"
    if coverage_missing.exists():
        for line in coverage_missing.read_text(encoding="utf-8-sig").splitlines():
            if not line.strip():
                continue
            rec = json.loads(line)
            key = rec.get("key", "")
            source = rec.get("source", "")
            value = merged.get(key)
            if isinstance(value, str):
                add_text(source, value)
            if source.strip():
                digest = _sha(source)
                sha_to_source[digest] = source

    # retry.jsonl from an earlier round also carries source text; keep it
    # resolvable so a repeated retry does not lose its target.
    retry_previous = work / "retry.jsonl"
    if retry_previous.exists():
        for line in retry_previous.read_text(encoding="utf-8-sig").splitlines():
            if not line.strip():
                continue
            rec = json.loads(line)
            source = rec.get("source", "")
            if source.strip():
                digest = _sha(source)
                sha_to_source[digest] = source

    # ---- text-addressed retry values (t:<sha>) --------------------------
    # `pipe_prep.py --from-retry` emits these. They are the only values allowed
    # to override an existing (bad) translation, including one item inside a
    # batch key, because the same paragraph may otherwise keep its broken value.
    # A text-fix value is ignored when a normal chunk covering the same source
    # is newer: that chunk is a deliberate re-translation, not a stale copy.
    retry_values = {
        k[2:]: v
        for k, v in merged.items()
        if k.startswith("t:") and isinstance(v, str) and v.strip()
    }
    retry_bad_tags: list[str] = []
    retry_applied = 0
    retry_overrides = 0
    retry_rebuilt = 0
    retry_ignored = 0
    ignored_fix_files: set[str] = set()

    # Drop stale values before rebuilding any key: the rebuild loop must not
    # see a value that was rejected for being older than a normal chunk.
    for digest in list(retry_values):
        if sha_to_source.get(digest) is None:
            continue
        normal_mtime = source_mtime.get(digest)
        fix_mtime = text_key_mtime.get(digest, 0)
        if normal_mtime is not None and fix_mtime < normal_mtime:
            retry_ignored += 1
            ignored_fix_files.add(owner.get("t:" + digest, "?"))
            del retry_values[digest]

    if retry_values:
        for digest, value in retry_values.items():
            source = sha_to_source.get(digest)
            if source is None:
                continue
            if _tag_seq(source) != _tag_seq(value):
                retry_bad_tags.append(source)
                continue
            retry_applied += 1
            previous = index.get(source)
            if previous is not None and previous != value:
                retry_overrides += 1
            index[source] = value

        for r in recs:
            key = r["key"]
            if r["kind"] == "batch":
                try:
                    inputs = _extract_json_input(r["prompt"])
                except ValueError:
                    continue
                if not any(_sha(it.get("input", "")) in retry_values for it in inputs):
                    continue
                outs = (
                    _parse_batch_output_map(merged.get(key))
                    if key in merged
                    else None
                )
                if outs is None:
                    outs = {}
                filled = []
                for it in inputs:
                    source = it.get("input", "")
                    out = retry_values.get(_sha(source))
                    if out is None or _tag_seq(source) != _tag_seq(out):
                        out = outs.get(it["id"])
                        if not (isinstance(out, str) and out.strip()):
                            out = index.get(source)
                    if out is None:
                        filled = None
                        break
                    filled.append({"id": it["id"], "output": out})
                if filled:
                    value = json.dumps(filled, ensure_ascii=False)
                    if merged.get(key) != value:
                        merged[key] = value
                        owner[key] = "<retry>"
                        retry_rebuilt += 1
            else:
                source = r["source"]
                out = retry_values.get(_sha(source))
                if out is None or _tag_seq(source) != _tag_seq(out):
                    continue
                if merged.get(key) != out:
                    merged[key] = out
                    owner[key] = "<retry>"
                    retry_rebuilt += 1

        # A source that now has a valid retry translation is no longer a
        # rejected text, even though the original chunk value was broken.
        index_bad_tags = [s for s in index_bad_tags if s not in index]

    if retry_ignored:
        print(
            f"[me] ignored {retry_ignored} stale text-fix value(s): "
            f"a normal chunk covering the same source is newer "
            f"({', '.join(sorted(ignored_fix_files))})"
        )

    # ---- fill keys nobody wrote, using identical source text ------------
    auto_filled = 0
    near_misses: list[tuple[str, str]] = []

    for r in recs:
        key = r["key"]
        if key in merged:
            continue
        if r["kind"] == "batch":
            try:
                inputs = _extract_json_input(r["prompt"])
            except ValueError:
                continue
            filled = []
            for item in inputs:
                text = item.get("input", "")
                if text in index:
                    filled.append({"id": item["id"], "output": index[text]})
                else:
                    filled = None
                    break
            if filled:
                merged[key] = json.dumps(filled, ensure_ascii=False)
                owner[key] = "<text-index>"
                auto_filled += 1
        else:
            source = r["source"]
            if source in index:
                merged[key] = index[source]
                owner[key] = "<text-index>"
                auto_filled += 1
            else:
                near = [t for t in (source,) if _tag_seq(t)]
                if near:
                    near_misses.append((key, source[:120]))

    # ---- validation -----------------------------------------------------
    tag_problems: list[tuple[str, str]] = []
    structural: list[str] = []
    gaps: "OrderedDict[str, list[str]]" = OrderedDict()
    content_warnings: list[dict] = []
    glossary_warnings: list[dict] = []

    def check_content(key: str, ident, source: str, output: str) -> None:
        if not output.strip():
            return
        flags = _content_flags(source, output, lang_out)
        if flags:
            content_warnings.append(
                {
                    "key": key,
                    "id": ident,
                    "flags": flags,
                    "source": source,
                    "output": output,
                }
            )

    def check_glossary(key: str, ident, source: str, output: str) -> None:
        """Advisory: a glossary term present in the source should appear in the
        target. The prompt asks the agent to use it; this only reports misses."""
        if not output.strip():
            return
        for src, dst, pattern in glossary:
            if not pattern.search(source):
                continue
            if dst.lower() in output.lower():
                continue
            glossary_warnings.append(
                {
                    "key": key,
                    "id": ident,
                    "term": src,
                    "expected": dst,
                    "source": source,
                    "output": output,
                }
            )

    retry_records: "OrderedDict[str, dict]" = OrderedDict()

    def add_retry(source: str, reason: str, key=None, ident=None) -> bool:
        if not source or not source.strip():
            return False
        digest = _sha(source)
        rec = retry_records.get(digest)
        if rec is None:
            rec = {
                "key": "t:" + digest,
                "kind": "text",
                "source": source,
                "reasons": [],
            }
            retry_records[digest] = rec
        label = reason
        if key:
            label += f" [{key}" + (f"#id{ident}]" if ident is not None else "]")
        if label not in rec["reasons"]:
            rec["reasons"].append(label)
        return True

    for r in recs:
        key = r["key"]
        if key not in merged:
            if r["kind"] == "batch":
                try:
                    texts = [it.get("input", "") for it in _extract_json_input(r["prompt"])]
                except ValueError:
                    texts = []
            else:
                texts = [r["source"]]
            bucket = gaps.setdefault(key, [])
            for t in texts:
                if t.strip() and t not in bucket:
                    bucket.append(t)
            continue

        if r["kind"] == "batch":
            try:
                inputs = _extract_json_input(r["prompt"])
            except ValueError:
                structural.append(f"{key}: input payload unparseable")
                continue
            outs = _parse_batch_output_map(merged[key])
            if outs is None:
                tag_problems.append((key, "output not a usable [{id,output}] array"))
                for item in inputs:
                    add_retry(item.get("input", ""), "unusable batch output", key)
                continue
            in_ids = [it.get("id") for it in inputs]
            if len(set(in_ids)) != len(in_ids):
                structural.append(f"{key}: duplicate ids in input")
            missing_ids = [i for i in in_ids if i not in outs]
            if missing_ids:
                structural.append(
                    f"{key}: {len(missing_ids)} id(s) missing from output (e.g. {missing_ids[:3]})"
                )
                by_id = {it.get("id"): it.get("input", "") for it in inputs}
                for ident in missing_ids:
                    add_retry(by_id.get(ident, ""), "missing id in output", key, ident)
            for item in inputs:
                ident = item.get("id")
                inp = item.get("input", "")
                out = outs.get(ident)
                if out is None:
                    if inp.strip():
                        bucket = gaps.setdefault(key, [])
                        if inp not in bucket:
                            bucket.append(inp)
                    continue
                if inp.strip() and not out.strip():
                    structural.append(f"{key}#id{ident}: empty translation")
                    add_retry(inp, "empty translation", key, ident)
                if _tag_seq(inp) != _tag_seq(out):
                    tag_problems.append(
                        (f"{key}#id{ident}", f"in={_tag_seq(inp)} out={_tag_seq(out)}")
                    )
                    add_retry(inp, "tag mismatch", key, ident)
                check_content(key, ident, inp, out)
                check_glossary(key, ident, inp, out)
        else:
            value = merged[key]
            if isinstance(value, list):
                value = value[0] if value else ""
            if not isinstance(value, str):
                structural.append(f"{key}: single value is {type(value).__name__}")
                add_retry(r["source"], f"value is {type(value).__name__}", key)
                continue
            if r["source"].strip() and not value.strip():
                structural.append(f"{key}: empty translation")
                add_retry(r["source"], "empty translation", key)
            if _tag_seq(r["source"]) != _tag_seq(value):
                tag_problems.append(
                    (key, f"in={_tag_seq(r['source'])} out={_tag_seq(value)}")
                )
                add_retry(r["source"], "tag mismatch", key)
            check_content(key, None, r["source"], value)
            check_glossary(key, None, r["source"], value)

    # ---- coverage stats -------------------------------------------------
    all_texts: "OrderedDict[str, None]" = OrderedDict()
    for r in recs:
        if r["kind"] == "batch":
            try:
                for it in _extract_json_input(r["prompt"]):
                    t = it.get("input", "")
                    if t.strip():
                        all_texts[t] = None
            except ValueError:
                pass
        else:
            if r["source"].strip():
                all_texts[r["source"]] = None
    covered_texts = sum(1 for t in all_texts if t in index)

    gap_texts: "OrderedDict[str, None]" = OrderedDict()
    for texts in gaps.values():
        for t in texts:
            if t.strip() and t not in index:
                gap_texts[t] = None

    for source in retry_bad_tags:
        add_retry(source, "retry output has wrong tag sequence")
    for text in gap_texts:
        add_retry(text, "missing translation")
    if args.retry_content:
        for warning in content_warnings:
            add_retry(
                warning["source"],
                "content: " + ",".join(warning["flags"]),
                warning["key"],
                warning["id"],
            )

    # Replay-time misses are the last source of untranslated segments: the
    # engine asked for a key (or plain-path text) that did not exist. Fold them
    # into the same retry file, skipping anything already covered now.
    retry_imported = 0
    if args.import_missing:
        for name, reason in (
            ("missing.jsonl", "replay miss"),
            ("missing_fallback.jsonl", "replay plain-path miss"),
        ):
            path = work / name
            if not path.exists():
                continue
            for line in path.read_text(encoding="utf-8-sig").splitlines():
                if not line.strip():
                    continue
                rec = json.loads(line)
                if name == "missing_fallback.jsonl":
                    source = rec.get("src", "")
                    if source and source not in index:
                        retry_imported += int(add_retry(source, reason))
                    continue
                if rec.get("kind") == "batch":
                    try:
                        inputs = _extract_json_input(rec.get("prompt", ""))
                    except ValueError:
                        continue
                    for item in inputs:
                        source = item.get("input", "")
                        if source and source not in index:
                            retry_imported += int(add_retry(source, reason))
                else:
                    source = rec.get("source", "")
                    if source and source not in index:
                        retry_imported += int(add_retry(source, reason))

    # ---- write artifacts ------------------------------------------------
    outputs = {
        k: v for k, v in merged.items() if k.startswith("b:") or k.startswith("s:")
    }
    (work / "llm_outputs.json").write_text(
        json.dumps(outputs, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    (work / "text_index.json").write_text(
        json.dumps({_sha(t): v for t, v in index.items()}, ensure_ascii=False, indent=1),
        encoding="utf-8",
    )
    with (work / "coverage_missing.jsonl").open("w", encoding="utf-8") as f:
        for text in gap_texts:
            f.write(
                json.dumps(
                    {"key": "t:" + _sha(text), "kind": "text", "source": text},
                    ensure_ascii=False,
                )
                + "\n"
            )
    content_warnings_path = work / "content_warnings.jsonl"
    if content_warnings:
        with content_warnings_path.open("w", encoding="utf-8") as f:
            for w in content_warnings:
                f.write(json.dumps(w, ensure_ascii=False) + "\n")
    elif content_warnings_path.exists():
        content_warnings_path.unlink()
    glossary_warnings_path = work / "glossary_warnings.jsonl"
    if glossary_warnings:
        with glossary_warnings_path.open("w", encoding="utf-8") as f:
            for w in glossary_warnings:
                f.write(json.dumps(w, ensure_ascii=False) + "\n")
    elif glossary_warnings_path.exists():
        glossary_warnings_path.unlink()
    retry_path = work / "retry.jsonl"
    if retry_records:
        with retry_path.open("w", encoding="utf-8") as f:
            for rec in retry_records.values():
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    elif retry_path.exists():
        retry_path.unlink()

    # Reuse every known translation to cover the plain do_translate fallback path.
    # do_translate looks up sha256(source text), exactly like text_index, so the
    # whole index can be written as-is. This guarantees that a replay-time
    # fallback still serves a translation instead of leaving English in the PDF.
    fb = {_sha(text): tr for text, tr in index.items() if tr.strip()}
    fb_path = work / "fallback_outputs.json"
    if fb:
        fb_path.write_text(
            json.dumps(fb, ensure_ascii=False, indent=1), encoding="utf-8"
        )
    elif fb_path.exists():
        fb_path.unlink()
    fb_written = len(fb)

    key_hits = sum(1 for k in kind_by_key if k in merged)
    report = {
        "keys_total": len(kind_by_key),
        "keys_covered": key_hits,
        "keys_agent": len([k for k in kind_by_key if k in merged and owner.get(k) != "<text-index>"]),
        "keys_text_auto": auto_filled,
        "texts_unique": len(all_texts),
        "texts_covered": covered_texts,
        "gaps_texts": len(gap_texts),
        "tag_problems": len(tag_problems),
        "retry_bad_tags": len(retry_bad_tags),
        "structural_problems": len(structural),
        "conflicts": len(conflicts),
        "unreadable_chunks": len(unreadable),
        "inconsistent_texts": len(inconsistent),
        "content_warnings": len(content_warnings),
        "content_warning_kinds": dict(
            Counter(f for w in content_warnings for f in w["flags"])
        ),
        "glossary_terms": len(glossary),
        "glossary_warnings": len(glossary_warnings),
        "glossary_missing_terms": len({w["term"] for w in glossary_warnings}),
        "retry_items": len(retry_records),
        "retry_applied": retry_applied,
        "retry_ignored": retry_ignored,
        "retry_overrides": retry_overrides,
        "retry_rebuilt_keys": retry_rebuilt,
        "retry_imported_from_missing": retry_imported,
        "text_fallbacks_written": fb_written,
        "extra_keys": len(extra_keys),
    }
    (work / "coverage_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8"
    )

    # ---- report ---------------------------------------------------------
    print(
        f"merged keys: {key_hits}/{len(kind_by_key)} "
        f"(agent {report['keys_agent']}, text-auto {auto_filled})"
    )
    print(
        f"unique source texts: {len(all_texts)}  covered: {covered_texts}  "
        f"gaps: {len(gap_texts)}"
    )
    if extra_keys:
        print(f"extra keys outside prompts.jsonl: {len(extra_keys)} (kept)")
    if conflicts:
        label = "CONFLICTING DUPLICATE KEYS" if args.conflict == "error" else "CONFLICTS RESOLVED"
        print(f"{label}: {len(conflicts)} (policy={args.conflict})")
        for k in conflicts[:10]:
            kept = owner.get(k, "?")
            ignored = [name for name in conflict_files.get(k, []) if name != kept]
            print(
                f"  {k}: kept {kept}, ignored "
                f"{', '.join(ignored) if ignored else '?'}"
            )
    if unreadable:
        print(f"UNREADABLE CHUNKS: {len(unreadable)}")
        for e in unreadable[:10]:
            print(f"  {e}")
    if inconsistent:
        print(f"TEXTS WITH DIVERGENT TRANSLATIONS: {len(inconsistent)} (first kept)")
    if index_bad_tags:
        print(f"TEXTS REJECTED FROM INDEX (tag mismatch): {len(index_bad_tags)}")
    print(f"\nTAG-SEQUENCE PROBLEMS: {len(tag_problems)}")
    for k, d in tag_problems[:40]:
        print(f"  {k}: {d}")
    if retry_bad_tags:
        print(f"\nRETRY VALUES WITH BAD TAGS: {len(retry_bad_tags)}")
        for source in retry_bad_tags[:8]:
            print(f"  {source[:120]!r}")
        print("  -> the normal value may be fine; the text-addressed retry value is not")
    if structural:
        print(f"\nSTRUCTURAL PROBLEMS: {len(structural)}")
        for s in structural[:40]:
            print(f"  {s}")
    if content_warnings:
        kinds = Counter(f for w in content_warnings for f in w["flags"])
        print(f"\nCONTENT WARNINGS: {len(content_warnings)} ({dict(kinds)})")
        for w in content_warnings[:8]:
            ident = f"#{w['id']}" if w["id"] is not None else ""
            print(f"  {w['key']}{ident}: {','.join(w['flags'])} {w['source'][:100]!r}")
        print("  -> inspect content_warnings.jsonl (translation quality, not structure)")
    if glossary_warnings:
        missing = sorted({w["term"] for w in glossary_warnings})
        print(
            f"\nGLOSSARY WARNINGS: {len(glossary_warnings)} "
            f"({len(missing)} term(s): {', '.join(missing[:8])})"
        )
        for w in glossary_warnings[:8]:
            print(
                f"  {w['key']}: {w['term']} => expected {w['expected']!r} "
                f"in {w['output'][:90]!r}"
            )
        print("  -> inspect glossary_warnings.jsonl (advisory; translation still ships)")
    if retry_applied or retry_rebuilt:
        print(
            f"\nRETRY FIXES APPLIED: {retry_applied} value(s) "
            f"({retry_overrides} override(s)), rebuilt {retry_rebuilt} key(s)"
        )
    if retry_records:
        reasons = Counter(
            reason.split(" [")[0]
            for rec in retry_records.values()
            for reason in rec["reasons"]
        )
        print(f"\nRETRY ITEMS: {len(retry_records)} ({dict(reasons)})")
        if retry_imported:
            print(f"  (imported {retry_imported} from replay miss logs)")
        for rec in list(retry_records.values())[:8]:
            print(f"  {rec['key']}: {', '.join(rec['reasons'])} {rec['source'][:90]!r}")
        print("  -> wrote retry.jsonl; translate only these with:")
        print("     python pipe_prep.py --from-retry retry.jsonl")
        print("     then merge again after llm_chunk_retry_*.json is written")
    print(f"\nCOVERAGE GAPS: {len(gap_texts)}")
    for t in list(gap_texts)[:10]:
        print(f"  {t[:140]!r}")
    if gap_texts:
        print(f"  -> wrote coverage_missing.jsonl ({len(gap_texts)} texts)")
        print("  -> translate them: python pipe_prep.py --from-missing coverage_missing.jsonl")

    print(
        f"\nwrote llm_outputs.json ({len(outputs)} keys), "
        f"text_index.json ({len(index)}), fallback_outputs.json ({fb_written})"
    )

    hard_fail = bool(
        unreadable
        or tag_problems
        or retry_bad_tags
        or structural
        or (conflicts and args.conflict == "error")
        or (args.strict and content_warnings)
    )
    soft_fail = bool(gap_texts) or retry_imported > 0
    if hard_fail or (soft_fail and not args.allow_gaps):
        print("\nRESULT: INCOMPLETE - fix the problems above before replay.")
        return 3
    if soft_fail:
        print(
            "\nRESULT: INCOMPLETE (gaps allowed by flag) - "
            "replay will leave that text untranslated."
        )
    else:
        print("\nRESULT: COMPLETE - every source text is covered.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
