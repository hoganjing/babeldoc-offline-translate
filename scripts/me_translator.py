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
        self._seen_prompt_keys: set[str] = set()
        self._seen_fb_srcs: set[str] = set()
        self._llm_outputs: dict[str, str] | None = None
        self._fb_outputs: dict[str, str] | None = None
        self.missing_llm_count = 0
        self.missing_fb_count = 0

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
        return json.loads(path.read_text(encoding="utf-8"))

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
        elif SINGLE_MARKER in text:
            source = _extract_single_source(text)
            key = "s:" + _sha(source)
            # Single path expects the plain translated text back, not JSON.
            collect_payload = source
            record = {"kind": "single", "key": key, "source": source}
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
        if output is not None:
            return output
        with self._lock:
            self.missing_fb_count += 1
        self._append_jsonl(
            "missing_fallback.jsonl", {"key": _sha(text), "src": text}
        )
        return text  # untranslated, logged for a fix-up pass
