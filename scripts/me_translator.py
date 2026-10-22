"""Offline LLM translator for BabelDOC (two-phase collect / replay).

The real translation is done offline by the agent (this skill's user). This
translator plugs into BabelDOC's BaseTranslator contract:

- ME_MODE=collect : dump every LLM prompt to <work>/prompts.jsonl, echo the
  input back as a syntactically valid ``[{id, output}, ...]`` array so the
  pipeline keeps running (translation quality of a collect run is irrelevant).
- ME_MODE=replay  : look up pre-translated outputs (llm_outputs.json keyed by
  sha256 of the canonical json_input) and return them; the single-paragraph
  path uses the same file keyed by sha256 of the source text.

Keying is on the json_input payload only, NOT the whole prompt, because
contextual-hint blocks (recent title) can legitimately differ between runs
while paragraph batch composition stays deterministic.

Work dir resolution: $ME_WORK_DIR, else ./me_work under the current working
directory. The driver (run_translation.py) sets ME_WORK_DIR before importing
this module.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
from pathlib import Path

from babeldoc.translator.translator import BaseTranslator

WORK_DIR = Path(
    os.environ.get("ME_WORK_DIR") or (Path.cwd() / "me_work")
).resolve()
MODE = os.environ.get("ME_MODE", "collect")  # collect | replay
INPUT_MARKER = "## Here is the input:"  # batch prompt (ILTranslatorLLMOnly)
SINGLE_MARKER = "Now translate the following text:"  # single prompt (ILTranslator fallback)


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _load_seen_keys(filename: str) -> set[str]:
    """Keys already recorded in a work-dir JSONL log.

    A collect process appends to prompts.jsonl / fallback_src.jsonl. Preloading
    the keys written by an earlier collect of the same work dir keeps a
    re-run (for example an extended page range) from duplicating records.
    """
    path = WORK_DIR / filename
    if not path.exists():
        return set()
    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    except OSError:
        return set()
    seen: set[str] = set()
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except ValueError:
            continue
        key = record.get("key") if isinstance(record, dict) else None
        if isinstance(key, str) and key:
            seen.add(key)
    return seen


def _extract_json_input(prompt: str) -> tuple[str, list]:
    """Return (canonical_json_str, parsed_list) of the batch payload.

    Walks every occurrence of the input marker from last to first: a paragraph
    could theoretically contain the marker string itself, in which case the
    last occurrence fails to decode and we fall through to the real one.
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
            canonical = json.dumps(obj, ensure_ascii=False, indent=2)
            return canonical, obj
    raise ValueError("no parseable json input array found in prompt")


def _extract_single_source(prompt: str) -> str:
    """Extract the source paragraph from a single-paragraph prompt.

    The template ends with ``Now translate the following text:\\n\\n$source``,
    so the source is the tail after the FIRST marker occurrence (a paragraph
    could theoretically contain the marker itself, which would only appear
    later in the string).
    """
    if SINGLE_MARKER not in prompt:
        raise ValueError("no single-prompt marker found in prompt")
    source = prompt.split(SINGLE_MARKER, 1)[1].lstrip("\n")
    if not source.strip():
        raise ValueError("empty source after single-prompt marker")
    return source


def _echo(parsed: list) -> str:
    """Build a valid collect-run response: same ids, input echoed as output."""
    echo = [
        {"id": item["id"], "output": item.get("input", "")} for item in parsed
    ]
    return json.dumps(echo, ensure_ascii=False)


class MeTranslator(BaseTranslator):
    name = "me-offline-llm"  # <= 20 chars (cache schema limit)

    def __init__(self, lang_in: str, lang_out: str, ignore_cache: bool = True):
        super().__init__(lang_in, lang_out, ignore_cache)
        self.model = "offline"  # BaseTranslator.__str__ references self.model
        self.mode = MODE
        WORK_DIR.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        # Collect appends to the logs; preload existing keys so a repeated
        # collect of the same work dir does not duplicate records.
        if self.mode == "collect":
            self._seen_prompt_keys: set[str] = _load_seen_keys("prompts.jsonl")
            self._seen_fb_srcs: set[str] = _load_seen_keys("fallback_src.jsonl")
        else:
            self._seen_prompt_keys = set()
            self._seen_fb_srcs = set()
        self._llm_outputs: dict[str, str] | None = None
        self._fb_outputs: dict[str, str] | None = None
        self._text_index: dict[str, str] | None = None
        self._text_index_enabled = os.environ.get("ME_TEXT_FALLBACK", "1") != "0"
        self.missing_llm_count = 0
        self.missing_fb_count = 0
        self.text_fallback_count = 0

    # ---------------- file helpers ----------------

    def _append_jsonl(self, filename: str, record: dict) -> None:
        path = WORK_DIR / filename
        with self._lock:
            with path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")

    def _load_json_map(self, filename: str) -> dict[str, str]:
        path = WORK_DIR / filename
        if not path.exists():
            return {}
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig"))
        except ValueError as exc:
            raise RuntimeError(f"cannot parse {path}: {exc}") from exc
        if not isinstance(data, dict):
            raise RuntimeError(f"{path} must contain a JSON object")
        return data

    # ---------------- text-keyed fallback ----------------
    #
    # BabelDOC keys a batch by the canonical json payload, so the same paragraph
    # can be requested under a new b: key when batch composition changes, or as
    # an s: key on a later run. llm_outputs.json alone cannot cover those. The
    # text index (written by pipe_merge.py) maps sha256(source text) -> the
    # translation of that EXACT text; it is only consulted after a key miss and
    # only exact byte matches are served, so <bN> tags can never be re-mapped.

    def _text_lookup(self, text: str) -> str | None:
        if not self._text_index_enabled:
            return None
        if self._text_index is None:
            with self._lock:
                if self._text_index is None:
                    self._text_index = self._load_json_map("text_index.json")
        return self._text_index.get(_sha(text))

    def _batch_text_fallback(self, parsed: list) -> str | None:
        fallback = []
        for item in parsed:
            hit = self._text_lookup(item.get("input", ""))
            if hit is None:
                return None
            fallback.append({"id": item["id"], "output": hit})
        return json.dumps(fallback, ensure_ascii=False)

    # ---------------- LLM batch path ----------------

    def do_llm_translate(self, text, rate_limit_params: dict = None) -> str:
        if text is None:
            # Capability probe from translator_supports_llm / ILTranslatorLLMOnly.
            # Must return normally: any exception is read as "not an LLM translator".
            return ""

        # --- route by prompt format -------------------------------------
        if INPUT_MARKER in text:
            canonical, parsed = _extract_json_input(text)
            key = "b:" + _sha(canonical)
            collect_payload = _echo(parsed)
            record = {"kind": "batch", "key": key, "prompt": text}
            single_source = None
        elif SINGLE_MARKER in text:
            single_source = _extract_single_source(text)
            key = "s:" + _sha(single_source)
            # Single path expects the plain translated text back, not JSON.
            collect_payload = single_source
            record = {"kind": "single", "key": key, "source": single_source}
        else:
            # Unknown shape: record it, let the engine handle the failure.
            self._append_jsonl("unparsed.jsonl", {"prompt": text})
            raise ValueError("unrecognized prompt format")

        if self.mode == "collect":
            with self._lock:
                first = key not in self._seen_prompt_keys
                self._seen_prompt_keys.add(key)
            if first:
                self._append_jsonl("prompts.jsonl", record)
            return collect_payload

        # replay
        if self._llm_outputs is None:
            self._llm_outputs = self._load_json_map("llm_outputs.json")
        output = self._llm_outputs.get(key)
        if output is not None:
            return output

        # Key miss: serve the translation of the identical source text, if any.
        # Same input text in, same output text out -- nothing is re-mapped.
        if single_source is None:
            fallback = self._batch_text_fallback(parsed)
        else:
            fallback = self._text_lookup(single_source)
        if fallback is not None:
            with self._lock:
                self.text_fallback_count += 1
            return fallback

        with self._lock:
            self.missing_llm_count += 1
        self._append_jsonl("missing.jsonl", {"key": key, **record})
        return collect_payload  # keep pipeline alive; segment stays untranslated

    # ---------------- per-paragraph fallback path ----------------

    def do_translate(self, text, rate_limit_params: dict = None) -> str:
        if text is None:
            return ""
        if self.mode == "collect":
            key = _sha(text)
            with self._lock:
                first = key not in self._seen_fb_srcs
                self._seen_fb_srcs.add(key)
            if first:
                self._append_jsonl("fallback_src.jsonl", {"key": key, "src": text})
            return text

        if self._fb_outputs is None:
            self._fb_outputs = self._load_json_map("fallback_outputs.json")
        output = self._fb_outputs.get(_sha(text))
        if output is None:
            # The engine can fire the plain path for a key that never reached
            # fallback_outputs.json (or when merge has not backfilled it yet).
            # The text index is keyed by the identical sha256(source), so an
            # exact match is safe to serve here too.
            output = self._text_lookup(text)
            if output is not None:
                with self._lock:
                    self.text_fallback_count += 1
        if output is not None:
            return output
        with self._lock:
            self.missing_fb_count += 1
        self._append_jsonl(
            "missing_fallback.jsonl", {"key": _sha(text), "src": text}
        )
        return text  # untranslated, logged for a fix-up pass
