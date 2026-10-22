#!/usr/bin/env python3
"""Split prompts.jsonl into translation chunks for parallel subagent dispatch.

Pure-pipe flow (v2.5): the agent translates the EXACT text babeldoc would send
to its LLM; the responses are merged straight into llm_outputs.json -- no
zh_texts / split_work / build_outputs / tag-reinjection intermediate. Because
the <bN> tags live inside the source text the agent sees, they can only be
preserved or dropped, never proportionally re-injected and misaligned.

Usage:
  python pipe_prep.py [--chunks N] [--max-chars N] [--seed zh_texts.json]
                      [--all] [--work DIR]
  python pipe_prep.py --from-missing coverage_missing.jsonl [--chunks N]
  python pipe_prep.py --from-retry retry.jsonl [--chunks N]

By default, records whose source text already has a non-empty translation in
text_index.json are skipped (incremental top-up after a partial or extended
collect); --all re-chunks everything.

Writes <work>/chunk_<i>.json (chunk_fix_<i>.json for --from-missing,
chunk_retry_<i>.json for --from-retry). Each:
  {"chunk": i, "instruction": <rules>, "output": "llm_chunk_<i>.json",
   "keys": ["..."], "records": [ <record>, ... ]}

A record is one of:
  batch : {"key": "b:...", "kind": "batch",
           "items": [{"id": N, "input": "<en with <bN>>", "seed": null|"<zh>"}, ...]}
  single: {"key": "s:...", "kind": "single", "source": "<en with <bN>>",
           "seed": null|"<zh>"}
  text  : {"key": "t:...", "kind": "text", "source": "<en with <bN>>"}
          (coverage / retry fix-ups from `pipe_merge.py`)

The subagent returns <work>/llm_chunk_<i>.json = {"<key>": <value>} where:
  batch  value = JSON array [{"id": N, "output": "<zh with <bN>>"}, ...]
  single value = plain zh string (with <bN>)
  text   value = plain zh string (with <bN>)
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
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
GLOSSARY_MAX_TERMS = 60

LANG_NAMES = {
    "en": "English",
    "zh": "Simplified Chinese",
    "zh-cn": "Simplified Chinese",
    "zh-tw": "Traditional Chinese",
    "ja": "Japanese",
    "ko": "Korean",
    "fr": "French",
    "de": "German",
    "es": "Spanish",
    "ru": "Russian",
    "pt": "Portuguese",
    "it": "Italian",
}


def _lang_name(code: str) -> str:
    return LANG_NAMES.get(code.strip().lower(), code)


def _extract_json_input(prompt: str) -> list:
    """Parse the batch payload array after the input marker.

    Mirrors me_translator._extract_json_input exactly (walks every marker
    occurrence last-to-first so a paragraph that literally contains the
    marker string still resolves to the real payload).
    """
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


def _strip(s: str) -> str:
    return TAG_RE.sub("", s or "")


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def covered_shas(work: Path) -> set[str]:
    """sha256(source) values that already have a non-empty translation."""
    covered: set[str] = set()
    index_path = work / "text_index.json"
    if index_path.exists():
        try:
            data = json.loads(index_path.read_text(encoding="utf-8-sig"))
            if isinstance(data, dict):
                for key, value in data.items():
                    if (
                        isinstance(key, str)
                        and isinstance(value, str)
                        and value.strip()
                    ):
                        covered.add(key)
        except (OSError, json.JSONDecodeError):
            pass
    # text_index.json is authoritative, but a workdir mid-loop may only have
    # singles in llm_outputs.json; s:<sha> carries the same per-text coverage.
    outputs_path = work / "llm_outputs.json"
    if outputs_path.exists():
        try:
            data = json.loads(outputs_path.read_text(encoding="utf-8-sig"))
            if isinstance(data, dict):
                for key, value in data.items():
                    if (
                        isinstance(key, str)
                        and key.startswith("s:")
                        and isinstance(value, str)
                        and value.strip()
                    ):
                        covered.add(key[2:])
        except (OSError, json.JSONDecodeError):
            pass
    return covered


def load_glossary(path: Path) -> list[tuple[str, str, re.Pattern]]:
    """Load {"source term": "target term"} and precompile safe matchers."""
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(data, dict):
        raise ValueError("glossary must be a JSON object {source: target}")
    terms: list[tuple[str, str, re.Pattern]] = []
    for src, dst in data.items():
        src = str(src).strip()
        dst = str(dst).strip()
        if not src or not dst:
            continue
        pattern = re.escape(src)
        if src[0].isascii() and src[0].isalnum():
            pattern = r"(?<![A-Za-z0-9])" + pattern
        if src[-1].isascii() and src[-1].isalnum():
            pattern = pattern + r"(?![A-Za-z0-9])"
        try:
            compiled = re.compile(pattern, re.IGNORECASE)
        except re.error:
            continue
        terms.append((src, dst, compiled))
    return terms


def select_terms(
    glossary: list[tuple[str, str, re.Pattern]], texts: list[str]
) -> list[tuple[str, str]]:
    """Terms that actually occur in this chunk, most frequent + longest first."""
    plain = "\n".join(_strip(t) for t in texts)
    if not plain.strip():
        return []
    hits: list[tuple[int, int, str, str]] = []
    for src, dst, pattern in glossary:
        count = len(pattern.findall(plain))
        if count:
            hits.append((count, len(src), src, dst))
    hits.sort(key=lambda h: (-h[0], -h[1], h[2].lower()))
    return [(src, dst) for _, _, src, dst in hits[:GLOSSARY_MAX_TERMS]]


def build_instruction(
    lang_in: str,
    lang_out: str,
    output_name: str,
    glossary_terms: list[tuple[str, str]] | None = None,
) -> str:
    src = _lang_name(lang_in)
    dst = _lang_name(lang_out)
    glossary_block = ""
    if glossary_terms:
        lines = "\n".join(f"   - {s} => {d}" for s, d in glossary_terms)
        glossary_block = (
            "\n7. 术语表（原文出现下列词时必须使用给定译法；大小写、单复数按目标语言习惯）：\n"
            f"{lines}\n"
        )
    return (
        f"你是专业 {src}->{dst} 翻译，逐条翻译下面的记录。铁律：\n"
        "1. 保留每一个 <bN> 标签（如 <b1>、</b3>，可能以不闭合单标签或成对标签出现）"
        "字节级不变：相同序列、相同相对位置，不得增删、改号或重排。\n"
        "2. 标签之间的文字照常翻译；标签本身是公式 / 富文本占位符，不要翻译标签本身。\n"
        "3. 段落数、条目顺序与 id 一律不变，不要合并或拆分条目。\n"
        "4. 若某记录带有 seed（旧参考译文），以其为质量基准，但必须按第 1 条重新放置标签。\n"
        f"5. 结果必须写入 {output_name}（UTF-8，无 BOM），顶层对象 {{\"<key>\": <value>}}，"
        "key 与输入完全一致。不要只把译文贴在聊天里。\n"
        "输出 value 的形式：\n"
        "  - batch 记录：JSON 数组 [{\"id\": N, \"output\": \"译文\"}]，id 与输入一致、"
        "不得遗漏任何 id，output 为对应 input 的译文（含 <bN>）。\n"
        "  - single / text 记录：纯字符串（含 <bN>）。\n"
        "6. 写入文件后，用 Python 读回该文件确认键集合与输入一致，再结束。"
        f"{glossary_block}"
        "只输出这一个 JSON 文件，不要附带任何解释文字。"
    )


def record_chars(task: dict) -> int:
    if task["kind"] == "batch":
        return sum(len(it.get("input", "")) for it in task["items"])
    return len(task.get("source", ""))


def record_texts(task: dict) -> list[str]:
    if task["kind"] == "batch":
        return [it.get("input", "") for it in task["items"]]
    return [task.get("source", "")]


def pack_chunks(tasks: list[dict], want: int, max_chars: int) -> list[list[dict]]:
    """Sequentially pack records so chunks are bounded by chars, not count.

    Records are never split (a batch key must be answered in one file).
    """
    if not tasks:
        return []
    total = sum(record_chars(t) for t in tasks)
    target = max(1, math.ceil(total / max(1, want)))
    chunks: list[list[dict]] = []
    cur: list[dict] = []
    cur_chars = 0
    for t in tasks:
        n = record_chars(t)
        if cur and (cur_chars + n > target or cur_chars + n > max_chars):
            chunks.append(cur)
            cur = []
            cur_chars = 0
        cur.append(t)
        cur_chars += n
    if cur:
        chunks.append(cur)
    return chunks


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--chunks",
        type=int,
        default=8,
        help="target number of parallel chunks (the --max-chars cap wins)",
    )
    ap.add_argument(
        "--max-chars",
        type=int,
        default=32000,
        help="hard cap on source characters per chunk (default 32000)",
    )
    ap.add_argument(
        "--all",
        action="store_true",
        help="re-chunk even sources already covered in text_index.json",
    )
    ap.add_argument(
        "--seed",
        default=None,
        help="optional old zh_texts.json to reuse as a quality seed",
    )
    fix_group = ap.add_mutually_exclusive_group()
    fix_group.add_argument(
        "--from-missing",
        default=None,
        help="chunk coverage_missing.jsonl (text-addressed gaps) instead of prompts.jsonl",
    )
    fix_group.add_argument(
        "--from-retry",
        default=None,
        help="chunk retry.jsonl (defective segments + gaps); these text-addressed "
        "values override the previous value on the next merge",
    )
    ap.add_argument("--work", default=None, help="override work dir (else $ME_WORK_DIR / ./me_work)")
    ap.add_argument(
        "--glossary",
        default=None,
        help="glossary JSON {source: target}; default <work>/glossary.json if present",
    )
    ap.add_argument(
        "--no-glossary",
        action="store_true",
        help="ignore any glossary (including <work>/glossary.json)",
    )
    args = ap.parse_args()

    work = Path(args.work).resolve() if args.work else WORK
    lang_in = os.environ.get("ME_LANG_IN", "en")
    lang_out = os.environ.get("ME_LANG_OUT", "zh")
    if args.from_retry:
        prefix = "retry_"
    elif args.from_missing:
        prefix = "fix_"
    else:
        prefix = ""

    glossary: list[tuple[str, str, re.Pattern]] = []
    if not args.no_glossary:
        gpath = Path(args.glossary) if args.glossary else work / "glossary.json"
        if not gpath.is_absolute():
            gpath = work / gpath
        if gpath.exists():
            try:
                glossary = load_glossary(gpath)
            except (OSError, ValueError, json.JSONDecodeError) as e:
                print(f"ERROR: glossary unreadable ({e})")
                return 2
            print(f"glossary: {len(glossary)} terms from {gpath.name}")
        elif args.glossary:
            print(f"ERROR: glossary not found: {gpath}")
            return 2

    seed_map: dict[str, str] = {}
    if args.seed:
        try:
            data = json.loads(Path(args.seed).read_text(encoding="utf-8-sig"))
            if isinstance(data, dict):
                for k, v in data.items():
                    if str(v).strip():
                        seed_map[_strip(str(k)).strip()] = str(v)
        except (OSError, json.JSONDecodeError) as e:
            print(f"WARNING: seed file unreadable ({e}) - skipping seeds")

    def seed_for(text: str):
        return seed_map.get(_strip(text).strip())

    tasks: list[dict] = []
    skipped = 0
    skipped_chars = 0

    fix_input = args.from_retry or args.from_missing
    if fix_input:
        src = Path(fix_input)
        if not src.is_absolute():
            src = work / src
        if not src.exists():
            print(f"ERROR: {src} not found - run pipe_merge.py first.")
            return 2
        for line in src.read_text(encoding="utf-8-sig").splitlines():
            if not line.strip():
                continue
            rec = json.loads(line)
            text = rec.get("source", "")
            key = rec.get("key", "")
            if not key or not text.strip():
                continue
            tasks.append(
                {
                    "key": key,
                    "kind": "text",
                    "source": text,
                    "seed": seed_for(text),
                }
            )
        if not tasks:
            label = "retry.jsonl" if args.from_retry else "coverage_missing.jsonl"
            print(f"nothing to translate: {label} is empty.")
            return 0
        kind = "retry fix-up" if args.from_retry else "coverage fix-up"
        print(f"{kind}: {len(tasks)} texts from {src.name}")
    else:
        prompts = work / "prompts.jsonl"
        if not prompts.exists():
            print(f"ERROR: {prompts} not found - run collect first.")
            return 2
        recs = [
            json.loads(l)
            for l in prompts.read_text(encoding="utf-8-sig").splitlines()
            if l.strip()
        ]
        covered = set() if args.all else covered_shas(work)
        for r in recs:
            key = r["key"]
            if r["kind"] == "batch":
                parsed = _extract_json_input(r["prompt"])
                items = [
                    {
                        "id": it["id"],
                        "input": it.get("input", ""),
                        "seed": seed_for(it.get("input", "")),
                    }
                    for it in parsed
                ]
                texts = [it["input"] for it in items]
                if (
                    covered
                    and texts
                    and all(t.strip() and _sha(t) in covered for t in texts)
                ):
                    skipped += 1
                    skipped_chars += sum(len(t) for t in texts)
                    continue
                tasks.append({"key": key, "kind": "batch", "items": items})
            else:
                src_text = r["source"]
                digest = _sha(src_text)
                if covered and digest in covered:
                    skipped += 1
                    skipped_chars += len(src_text)
                    continue
                tasks.append(
                    {
                        "key": key,
                        "kind": "single",
                        "source": src_text,
                        "seed": seed_for(src_text),
                    }
                )
        if skipped:
            print(
                f"incremental: skipped {skipped} record(s) already translated "
                f"({skipped_chars} chars); --all re-chunks everything"
            )

    if not tasks:
        print(
            "nothing to translate: every prompt record is already covered "
            "(use --all to re-chunk)."
        )
        return 0

    want = args.chunks
    if want < 1:
        print("ERROR: --chunks must be >= 1")
        return 2

    chunks = pack_chunks(tasks, want, max(1, args.max_chars))

    sizes = []
    injected_chunks = 0
    injected_terms: set[str] = set()
    for i, ch in enumerate(chunks):
        out_name = f"llm_chunk_{prefix}{i}.json"
        terms = (
            select_terms(glossary, [t for task in ch for t in record_texts(task)])
            if glossary
            else []
        )
        if terms:
            injected_chunks += 1
            injected_terms.update(src for src, _ in terms)
        payload = {
            "chunk": i,
            "instruction": build_instruction(lang_in, lang_out, out_name, terms),
            "output": out_name,
            "keys": [t["key"] for t in ch],
            "records": ch,
        }
        (work / f"chunk_{prefix}{i}.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8"
        )
        sizes.append(
            {
                "chunk": i,
                "n_records": len(ch),
                "n_chars": sum(record_chars(t) for t in ch),
                "n_glossary_terms": len(terms),
                "output": out_name,
            }
        )

    meta_name = f"chunks_{prefix}meta.json" if prefix else "chunks_meta.json"
    (work / meta_name).write_text(
        json.dumps(sizes, ensure_ascii=False, indent=1), encoding="utf-8"
    )

    total_chars = sum(s["n_chars"] for s in sizes)
    print(
        f"wrote {len(chunks)} chunks; total records {len(tasks)}, "
        f"source chars {total_chars}"
    )
    if glossary:
        print(
            f"glossary: injected {len(injected_terms)} terms into "
            f"{injected_chunks}/{len(chunks)} chunks (cap {GLOSSARY_MAX_TERMS}/chunk)"
        )
    for s in sizes:
        print(
            f"  chunk {s['chunk']}: {s['n_records']} records, "
            f"{s['n_chars']} chars -> {s['output']}"
        )
    oversized = [s for s in sizes if s["n_chars"] > args.max_chars]
    if oversized:
        print(
            "WARNING: some chunks exceed --max-chars (a single record is larger "
            f"than the cap): {[s['chunk'] for s in oversized]}"
        )
    if len(chunks) != want:
        print(
            f"note: --chunks {want} is a target; --max-chars "
            f"{args.max_chars} produced {len(chunks)} chunks."
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
