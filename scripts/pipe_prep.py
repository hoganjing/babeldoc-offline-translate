#!/usr/bin/env python3
"""Split prompts.jsonl into translation chunks for parallel subagent dispatch.

Pure-pipe flow (v2.5): the agent translates the EXACT text babeldoc would send
to its LLM; the responses are merged straight into llm_outputs.json -- no
zh_texts / split_work / build_outputs / tag-reinjection intermediate. Because
the <bN> tags live inside the source text the agent sees, they can only be
preserved or dropped, never proportionally re-injected and misaligned.

Usage:
  python pipe_prep.py [--chunks N] [--seed zh_texts.json] [--work DIR]

Writes <work>/chunk_<i>.json. Each chunk is:
  {"chunk": i, "instruction": <rules>, "records": [ <record>, ... ]}

A record is one of:
  batch : {"key": "b:...", "kind": "batch",
           "items": [{"id": N, "input": "<en with <bN>>", "seed": null|"<zh>"}, ...]}
  single: {"key": "s:...", "kind": "single", "source": "<en with <bN>>",
           "seed": null|"<zh>"}

The subagent returns <work>/llm_chunk_<i>.json = {"<key>": <value>} where:
  batch  value = JSON array [{"id": N, "output": "<zh with <bN>>"}, ...]
  single value = plain zh string (with <bN>)
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

INPUT_MARKER = "## Here is the input:"
TAG_RE = re.compile(r"</?b\d+>")


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


INSTRUCTION = (
    "你是专业 EN->ZH 翻译，逐条翻译下面的记录。铁律：\n"
    "1. 保留每一个 <bN> 标签（如 <b1>、</b3>，可能以不闭合单标签或成对标签出现）"
    "字节级不变：相同序列、相同相对位置，不得增删、改号或重排。\n"
    "2. 标签之间的文字照常翻译；标签本身是公式 / 富文本占位符，不要翻译标签本身。\n"
    "3. 段落数、条目顺序与 id 一律不变，不要合并或拆分条目。\n"
    "4. 若某记录带有 seed（旧参考译文），以其为质量基准，但必须按第 1 条重新放置标签。\n"
    "输出：写一个 JSON 文件，顶层对象 {\"<key>\": <value>}，其中：\n"
    "  - batch 记录：value 是 JSON 数组 [{\"id\": N, \"output\": \"中文译文\"}]，"
    "id 与输入一致，output 为对应 input 的译文（含 <bN>）。\n"
    "  - single 记录：value 是纯中文字符串（含 <bN>）。\n"
    "只输出这一个 JSON 文件，不要附带任何解释文字。"
)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--chunks", type=int, default=8, help="number of parallel chunks")
    ap.add_argument(
        "--seed",
        default=None,
        help="optional old zh_texts.json to reuse as a quality seed",
    )
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

    seed_map: dict[str, str] = {}
    if args.seed:
        try:
            data = json.loads(Path(args.seed).read_text(encoding="utf-8"))
            if isinstance(data, dict):
                for k, v in data.items():
                    if str(v).strip():
                        seed_map[_strip(str(k)).strip()] = str(v)
        except (OSError, json.JSONDecodeError) as e:
            print(f"WARNING: seed file unreadable ({e}) - skipping seeds")

    def seed_for(text: str):
        return seed_map.get(_strip(text).strip())

    tasks = []
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
            tasks.append({"key": key, "kind": "batch", "items": items})
        else:
            src = r["source"]
            tasks.append(
                {"key": key, "kind": "single", "source": src, "seed": seed_for(src)}
            )

    chunks = [[] for _ in range(args.chunks)]
    for i, t in enumerate(tasks):
        chunks[i % args.chunks].append(t)

    for i, ch in enumerate(chunks):
        payload = {"chunk": i, "instruction": INSTRUCTION, "records": ch}
        (work / f"chunk_{i}.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8"
        )
    meta = [{"chunk": i, "n_records": len(ch)} for i, ch in enumerate(chunks)]
    (work / "chunks_meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    print(f"wrote {args.chunks} chunks; total records {len(tasks)}")
    for m in meta:
        print(f"  chunk {m['chunk']}: {m['n_records']} records")
    return 0


if __name__ == "__main__":
    sys.exit(main())
