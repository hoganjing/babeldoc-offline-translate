#!/usr/bin/env python3
"""Offline self-test for the pure-pipe scripts (no PDF, BabelDOC or network).

Usage:
  python selftest.py [--work DIR] [--keep]

Always builds a synthetic workdir under %TEMP% and exercises:
  merge -> retry.jsonl -> pipe_prep --from-retry -> merge -> complete
  content/glossary warnings, stale-retry rejection, archive cleanup,
  incremental prep, --all re-chunking, exact-text fallback, preflight,
  language identity + page ranges, retry-tag gating, prompt dedupe,
  single-marker parsing, and the Windows short-path helpers.

--work additionally audits a real work dir (read-only): artifact parse,
preflight coverage, retry.jsonl absent, fallback coverage.

Exit code 0 = all checks passed, 1 = at least one failed.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROMPT_MARKER = "## Here is the input:"
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _write_json(path: Path, data) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")


def _run(script: str, *args) -> subprocess.CompletedProcess:
    cmd = [sys.executable, str(SCRIPT_DIR / script), *map(str, args)]
    env = dict(os.environ)
    env["PYTHONUTF8"] = "1"
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        check=False,
    )


def _need(cond: bool, message: str, proc: subprocess.CompletedProcess | None = None) -> None:
    if cond:
        return
    detail = message
    if proc is not None:
        detail += f"\n--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}"
    raise AssertionError(detail)


def _prompts(work: Path) -> list[dict]:
    path = work / "prompts.jsonl"
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8-sig").splitlines()
        if line.strip()
    ]


def _report(work: Path) -> dict:
    return json.loads((work / "coverage_report.json").read_text(encoding="utf-8"))


def test_archive(tmp: Path) -> None:
    """Switching inputs must archive every per-run artifact, not just the old set."""
    import run_translation as rt

    work = tmp / "archive_case"
    work.mkdir()
    for name in ("working_collect", "working_replay"):
        (work / name).mkdir()
    expected = [
        "prompts.jsonl",
        "src_texts.json",
        "zh_texts.json",
        "llm_outputs.json",
        "text_index.json",
        "fallback_outputs.json",
        "fallback_src.jsonl",
        "retry.jsonl",
        "coverage_missing.jsonl",
        "coverage_report.json",
        "chunks_meta.json",
        "chunks_retry_meta.json",
        "chunks_fix_meta.json",
        "content_warnings.jsonl",
        "glossary_warnings.jsonl",
        "run_stamp.json",
        "chunk_0.json",
        "chunk_retry_0.json",
        "llm_chunk_0.json",
        "llm_chunk_retry_0.json",
        "llm_chunk_fix_0.json",
        "zh_slice_0.json",
        "missing.jsonl",
        "missing_fallback.jsonl",
        "unparsed.jsonl",
    ]
    for name in expected:
        (work / name).write_text("{}", encoding="utf-8")
    for name in ("out_collect", "out_replay"):
        out = work / name
        out.mkdir()
        (out / "x.pdf").write_text("x", encoding="utf-8")

    old_work = rt.WORK
    rt.WORK = work
    try:
        dest = rt._archive_previous({"sha256": "deadbeef"})
    finally:
        rt.WORK = old_work

    for name in expected:
        _need(not (work / name).exists(), f"{name} was not archived")
        _need((dest / name).exists(), f"{name} missing from archive")
    for name in ("out_collect", "out_replay"):
        _need(not (work / name).exists(), f"{name} was not archived")
        _need((dest / name / "x.pdf").exists(), f"{name} missing from archive")
    _need(not (work / "working_collect").exists(), "working_collect not removed")
    _need(not (work / "working_replay").exists(), "working_replay not removed")


def test_language_identity(tmp: Path) -> None:
    """Language changes and non-extending page ranges must archive, not append."""
    import run_translation as rt

    pdf = tmp / "identity.pdf"
    pdf.write_bytes(b"%PDF-1.4 selftest identity")
    en_zh = rt._fingerprint(str(pdf), "1-5", "en", "zh")
    en_ja = rt._fingerprint(str(pdf), "1-5", "en", "ja")
    fr_zh = rt._fingerprint(str(pdf), "1-5", "fr", "zh")
    _need(en_zh["lang_in"] == "en", "lang_in missing from fingerprint")
    _need(en_zh["lang_out"] == "zh", "lang_out missing from fingerprint")
    base = {
        "sha256": en_zh["sha256"],
        "pages": "1-5",
        "lang_in": "en",
        "lang_out": "zh",
    }
    _need(rt._collect_action(base, en_zh) == "append", "same identity must append")
    _need(rt._collect_action(base, en_ja) == "archive", "lang_out change must archive")
    _need(rt._collect_action(base, fr_zh) == "archive", "lang_in change must archive")
    legacy = {"sha256": en_zh["sha256"], "pages": "1-5", "lang_out": "zh"}
    _need(
        rt._collect_action(legacy, en_zh) == "append",
        "legacy stamp without lang_in must append",
    )
    _need(rt._range_extends("1-5", "1-7") is True, "extension not detected")
    _need(rt._range_extends("1-5", "1-5,9") is True, "comma extension not detected")
    _need(rt._range_extends("1-5", "2-4") is False, "shrink not detected")
    _need(rt._range_extends(None, "1-5") is False, "ALL -> subset must archive")
    _need(rt._range_extends("1-5", None) is True, "subset -> ALL must extend")
    _need(rt._range_extends("1-5", "1-5") is True, "identical range must extend")
    _need(
        rt._range_extends("1-5", "bogus") is None,
        "unparseable range must be undecidable",
    )
    shrunk = rt._fingerprint(str(pdf), "2-4", "en", "zh")
    _need(
        rt._collect_action(base, shrunk) == "archive",
        "page-range shrink must archive",
    )


def test_pipeline(tmp: Path) -> None:
    work = tmp / "pipe"
    work.mkdir()

    s1 = "The motor cortex controls voluntary movement in the human body."
    s2 = "Synergy is a neural strategy that stabilizes performance during movement."
    s3 = (
        "The stability of a coordinated movement emerges from the interaction "
        "of many components over time."
    )
    item1 = "Motor learning is a process of adaptation."
    item2 = "The <b1>synergy</b1> stabilizes performance."
    s1_key = "s:" + _sha(s1)
    s2_key = "s:" + _sha(s2)
    s3_key = "s:" + _sha(s3)
    s4_key = "s:" + _sha(item1)
    batch_key = "b:selftest"
    s1_out = "运动皮层控制人体的自主运动。"
    s2_out = "协同是一种在运动中稳定表现的中枢策略。"
    item1_out = "运动学习是一个适应过程。"
    item2_bad = "协同<b2>稳定</b2>表现。"

    recs = [
        {"key": s1_key, "kind": "single", "source": s1},
        {"key": s2_key, "kind": "single", "source": s2},
        {"key": s3_key, "kind": "single", "source": s3},
        {"key": s4_key, "kind": "single", "source": item1},
        {
            "key": batch_key,
            "kind": "batch",
            "prompt": PROMPT_MARKER
            + "\n"
            + json.dumps(
                [{"id": 1, "input": item1}, {"id": 2, "input": item2}],
                ensure_ascii=False,
            ),
        },
    ]
    (work / "prompts.jsonl").write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in recs) + "\n",
        encoding="utf-8",
    )
    _write_json(
        work / "llm_chunk_0.json",
        {
            s1_key: s1_out,
            s2_key: s2_out,
            s3_key: s3,  # identical source -> content warning
            s4_key: item1_out,
            batch_key: [
                {"id": 1, "output": item1_out},
                {"id": 2, "output": item2_bad},  # b2 should be b1 -> retry
            ],
        },
    )
    _write_json(work / "glossary.json", {"motor": "马达", "synergy": "协同"})

    first = _run("pipe_merge.py", "--work", work)
    _need(first.returncode == 3, "merge should fail on the tag mismatch", first)
    _need((work / "retry.jsonl").exists(), "retry.jsonl was not written")
    report = _report(work)
    _need(report["tag_problems"] == 1, f"expected 1 tag problem, got {report}")
    _need(report["content_warnings"] >= 1, f"expected content warning, got {report}")
    _need(report["glossary_warnings"] >= 1, f"expected glossary warning, got {report}")
    retry = [
        json.loads(line)
        for line in (work / "retry.jsonl").read_text(encoding="utf-8-sig").splitlines()
        if line.strip()
    ]
    _need(len(retry) == 1 and retry[0]["source"] == item2, "wrong retry payload")
    _need(
        "GLOSSARY WARNINGS" in first.stdout,
        "merge did not report glossary warnings",
        first,
    )

    prep = _run("pipe_prep.py", "--work", work, "--from-retry", "retry.jsonl")
    _need(prep.returncode == 0, "pipe_prep --from-retry failed", prep)
    retry_chunk = json.loads((work / "chunk_retry_0.json").read_text(encoding="utf-8"))
    _need(retry_chunk["output"] == "llm_chunk_retry_0.json", "wrong retry output name")
    _need(
        len(retry_chunk["records"]) == 1
        and retry_chunk["records"][0]["kind"] == "text",
        f"unexpected retry records: {retry_chunk['records']}",
    )
    t_key = retry_chunk["records"][0]["key"]
    _write_json(work / "llm_chunk_retry_0.json", {t_key: "协同<b1>稳定</b1>表现。"})

    second = _run("pipe_merge.py", "--work", work)
    _need(second.returncode == 0, "retry merge should complete", second)
    _need(not (work / "retry.jsonl").exists(), "retry.jsonl should be removed")
    report = _report(work)
    _need(report["tag_problems"] == 0, f"tag problems remain: {report}")
    _need(report["gaps_texts"] == 0, f"gaps remain: {report}")
    _need(report["retry_applied"] == 1, f"retry value not applied: {report}")
    outputs = json.loads((work / "llm_outputs.json").read_text(encoding="utf-8"))
    batch_out = json.loads(outputs[batch_key])
    _need(
        [row["output"] for row in batch_out]
        == [item1_out, "协同<b1>稳定</b1>表现。"],
        f"batch was not rebuilt from retry: {batch_out}",
    )
    _need(
        (work / "llm_chunk_retry_0.json").exists(),
        "retry file should stay: it is the source of the applied fix",
    )

    # A newer normal chunk is a deliberate re-translation and must win over an
    # older retry value for the same source.
    normal_chunk = work / "llm_chunk_0.json"
    normal = json.loads(normal_chunk.read_text(encoding="utf-8"))
    normal[batch_key] = [
        {"id": 1, "output": item1_out},
        {"id": 2, "output": "协同作用<b1>稳定</b1>了表现。"},
    ]
    _write_json(normal_chunk, normal)
    stale_path = work / "llm_chunk_retry_0.json"
    _write_json(stale_path, {t_key: "协同<b1>保持</b1>稳定。"})
    stale_mtime = normal_chunk.stat().st_mtime - 10
    os.utime(stale_path, (stale_mtime, stale_mtime))

    stale = _run("pipe_merge.py", "--work", work)
    _need(stale.returncode == 0, "stale retry merge should complete", stale)
    report = _report(work)
    _need(report["retry_applied"] == 0, f"stale retry value was applied: {report}")
    _need(report["retry_ignored"] == 1, f"stale retry value was not ignored: {report}")
    _need("ignored 1 stale" in stale.stdout, "no stale-retry warning", stale)
    outputs = json.loads((work / "llm_outputs.json").read_text(encoding="utf-8"))
    batch_out = json.loads(outputs[batch_key])
    _need(
        [row["output"] for row in batch_out]
        == [item1_out, "协同作用<b1>稳定</b1>了表现。"],
        f"stale retry overrode the newer translation: {batch_out}",
    )

    # A plain merge clears the advisory retry list (no defects remain).
    final = _run("pipe_merge.py", "--work", work)
    _need(final.returncode == 0, "final clean merge failed", final)
    _need(not (work / "retry.jsonl").exists(), "clean merge kept retry.jsonl")

    # Incremental default: everything is in text_index.json.
    inc = _run("pipe_prep.py", "--work", work)
    _need(inc.returncode == 0, "incremental prep failed", inc)
    _need("nothing to translate" in inc.stdout, "expected an empty incremental run", inc)

    # --all re-chunks everything; a small target still keeps the batch record.
    for path in work.glob("chunk_[0-9]*.json"):
        path.unlink()
    full = _run(
        "pipe_prep.py",
        "--work",
        work,
        "--all",
        "--chunks",
        "2",
    )
    _need(full.returncode == 0, "--all prep failed", full)
    chunk_files = sorted(work.glob("chunk_[0-9]*.json"))
    _need(
        len(chunk_files) >= 2,
        f"--chunks 2 should produce >=2 chunks, got {len(chunk_files)}",
    )
    all_records = []
    for path in chunk_files:
        all_records.extend(json.loads(path.read_text(encoding="utf-8"))["records"])
    batch_records = [r for r in all_records if r["kind"] == "batch"]
    _need(len(batch_records) == 1, "batch record missing from auto prep")
    _need(
        len(batch_records[0]["items"]) == 2,
        f"batch items changed unexpectedly: {batch_records[0]['items']}",
    )

    # Preflight must see full coverage of the source texts.
    sys.path.insert(0, str(SCRIPT_DIR))
    from replay_precheck import precheck

    result = precheck(work)
    _need(not result["missing"], f"preflight missing: {result}")
    _need(result.get("fallback_missing", 0) == 0, f"preflight fallback: {result}")

    # A bad text-addressed retry value must gate the merge even when the normal
    # batch value is fine: exit 3 plus a retry item, never a false COMPLETE.
    (work / "llm_chunk_retry_0.json").unlink()
    _write_json(
        work / "llm_chunk_retry_bad.json",
        {"t:" + _sha(item2): "协同<b9>稳定</b9>表现。"},
    )
    bad_retry = _run("pipe_merge.py", "--work", work)
    _need(bad_retry.returncode == 3, "bad retry tags must fail the merge", bad_retry)
    report = _report(work)
    _need(
        report.get("retry_bad_tags") == 1,
        f"retry_bad_tags not reported: {report}",
    )
    _need((work / "retry.jsonl").exists(), "bad retry item missing from retry.jsonl")
    _need(
        "RETRY VALUES WITH BAD TAGS" in bad_retry.stdout,
        "merge did not report the bad retry value",
        bad_retry,
    )


def test_short_path(tmp: Path) -> None:
    import run_translation as rt

    _need(len(rt._safe_stem("x" * 200)) <= 80, "safe stem did not truncate")
    _need(rt._safe_stem("a/b:c") == "a_b_c", "safe stem did not sanitize")
    dummy = tmp / "dummy.pdf"
    dummy.write_bytes(b"%PDF-1.4 selftest")
    short = rt._short_run(str(dummy), "selftest0", "collect")
    if os.name == "nt":
        _need(short is not None, "short-path copy was not created")
        _need(Path(short[0]).exists(), "short-path PDF missing")
        src_out = Path(short[1])
        src_out.mkdir(parents=True, exist_ok=True)
        (src_out / "dummy.no_watermark.zh.dual.pdf").write_bytes(b"fake")
        dest = tmp / "restored"
        copied = rt._restore_outputs(src_out, dest)
        _need(copied == 1, "restore did not copy the PDF")
        _need(
            (dest / "dummy.no_watermark.zh.dual.pdf").exists(),
            "restored PDF missing",
        )
        shutil.rmtree(Path(tempfile.gettempdir()) / "me_short" / "selftest0",
                      ignore_errors=True)


def test_text_fallback(tmp: Path) -> None:
    """Replay key misses must be served only by an exact source-text hit."""
    import threading
    import types

    stubs = {
        "babeldoc": types.ModuleType("babeldoc"),
        "babeldoc.translator": types.ModuleType("babeldoc.translator"),
        "babeldoc.translator.translator": types.ModuleType(
            "babeldoc.translator.translator"
        ),
    }
    stubs["babeldoc"].__path__ = []
    stubs["babeldoc.translator"].__path__ = []

    class _BaseTranslator:
        pass

    stubs["babeldoc.translator.translator"].BaseTranslator = _BaseTranslator
    previous = {name: sys.modules.get(name) for name in stubs}
    sys.modules.update(stubs)
    try:
        if str(SCRIPT_DIR) not in sys.path:
            sys.path.insert(0, str(SCRIPT_DIR))
        import me_translator as mt
    finally:
        for name, mod in previous.items():
            if mod is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = mod

    src = "The <b1>synergy</b1> stabilizes performance."
    out = "协同<b1>稳定</b1>表现。"
    work = tmp / "fallback"
    work.mkdir()
    (work / "text_index.json").write_text(
        json.dumps({mt._sha(src): out}, ensure_ascii=False), encoding="utf-8"
    )
    old_work = mt.WORK_DIR
    mt.WORK_DIR = work
    try:
        # Collect appends to these logs; preloading must dedupe a re-run.
        (work / "prompts.jsonl").write_text(
            '{"key": "b:old", "kind": "batch"}\n'
            "not json\n"
            '{"key": "s:old", "kind": "single"}\n',
            encoding="utf-8",
        )
        (work / "fallback_src.jsonl").write_text(
            '{"key": "fb-old", "src": "x"}\n', encoding="utf-8"
        )
        _need(
            mt._load_seen_keys("prompts.jsonl") == {"b:old", "s:old"},
            "prompt key preload is wrong",
        )
        _need(
            mt._load_seen_keys("fallback_src.jsonl") == {"fb-old"},
            "fallback key preload is wrong",
        )
        t = object.__new__(mt.MeTranslator)
        t._lock = threading.Lock()
        t._text_index_enabled = True
        t._text_index = None
        _need(t._text_lookup(src) == out, "exact-text lookup missed")
        _need(t._text_lookup("absent") is None, "unknown text should miss")
        one = t._batch_text_fallback([{"id": 1, "input": src}])
        _need(one is not None, "batch fallback missed a fully covered batch")
        _need(
            json.loads(one) == [{"id": 1, "output": out}],
            f"wrong batch fallback: {one}",
        )
        partial = t._batch_text_fallback(
            [{"id": 1, "input": src}, {"id": 2, "input": "absent"}]
        )
        _need(partial is None, "partial batch fallback must be all-or-nothing")
        t._text_index_enabled = False
        _need(t._text_lookup(src) is None, "ME_TEXT_FALLBACK=0 should disable lookup")
    finally:
        mt.WORK_DIR = old_work


def test_single_marker(tmp: Path) -> None:
    """The first marker belongs to the template; a later one is source text."""
    sys.path.insert(0, str(SCRIPT_DIR))
    import pipe_status
    from replay_precheck import _single_source as precheck_source

    tail = "Body text Now translate the following text: tail"
    prompt = "Intro Now translate the following text:\n\n" + tail
    _need(
        precheck_source(prompt) == tail,
        f"precheck truncated at the last marker: {precheck_source(prompt)!r}",
    )
    work = tmp / "marker"
    work.mkdir()
    (work / "prompts.jsonl").write_text(
        json.dumps(
            {"key": "s:x", "kind": "single", "prompt": prompt},
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    stats = pipe_status._prompt_stats(work)
    _need(
        stats["source_chars"] == len(tail),
        f"pipe_status used the wrong marker: {stats}",
    )


def test_workdir(work: Path) -> None:
    required = ("prompts.jsonl", "llm_outputs.json", "text_index.json")
    for name in required:
        _need((work / name).exists(), f"missing {name}")
    for name in ("llm_outputs.json", "text_index.json", "coverage_report.json"):
        path = work / name
        _need(path.exists(), f"missing {name}")
        json.loads(path.read_text(encoding="utf-8-sig"))
    report = _report(work)
    _need(not (work / "retry.jsonl").exists(), "retry.jsonl is still present")
    _need(report.get("retry_items", 0) == 0, f"retry items remain: {report}")
    sys.path.insert(0, str(SCRIPT_DIR))
    from replay_precheck import precheck

    result = precheck(work)
    _need(not result["missing"], f"preflight missing: {result}")
    _need(result.get("fallback_missing", 0) == 0, f"preflight fallback: {result}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--work", default=None, help="also audit this real work dir")
    ap.add_argument("--keep", action="store_true", help="keep the synthetic work dir")
    args = ap.parse_args()

    tmp = Path(tempfile.mkdtemp(prefix="me_selftest_"))
    failures = 0
    tests = [
        ("pipeline", lambda: test_pipeline(tmp)),
        ("archive", lambda: test_archive(tmp)),
        ("language-identity", lambda: test_language_identity(tmp)),
        ("short-path", lambda: test_short_path(tmp)),
        ("text-fallback", lambda: test_text_fallback(tmp)),
        ("single-marker", lambda: test_single_marker(tmp)),
    ]
    if args.work:
        tests.append(("workdir", lambda: test_workdir(Path(args.work).resolve())))
    try:
        for name, fn in tests:
            try:
                fn()
            except Exception as exc:  # noqa: BLE001 - report any test failure
                failures += 1
                print(f"FAIL {name}: {exc}")
            else:
                print(f"PASS {name}")
    finally:
        if args.keep:
            print(f"synthetic work dir kept: {tmp}")
        else:
            shutil.rmtree(tmp, ignore_errors=True)

    print(f"\n{'PASS' if not failures else 'FAIL'}: {len(tests) - failures}/{len(tests)} checks")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
