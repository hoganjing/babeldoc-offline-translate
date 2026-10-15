"""Runtime monkeypatch: keep pass-through text above its white OCR mask.

Why (root cause verified against babeldoc sources):
    In ocr_workaround mode every original char is wiped to render_order=None,
    sub_render_order=0 (il_creater.py), and a white fill rectangle is appended
    per paragraph without render_order (paragraph_finder.
    add_text_fill_background). At render-unit build time
    (pdf_creater.create_render_units_for_page) both fall back to
    render_order=9999999999999999, so the sort degenerates to
    sub_render_order:
      * pass-through chars of UNTRANSLATED paragraphs (below min_text_length,
        never sent to the LLM) keep sub_render_order=0 -> painted BEFORE their
        own mask (mask sub = enumerate index >= 0) -> the mask erases them:
        page numbers, short headings, lone punctuation vanish;
      * translated chars are created without render_order/sub_render_order
        -> None -> 9999999999999999 -> painted AFTER the mask -> body text
        survives (the designed behavior pass-through text should also have).

Fix (char-side, NOT rect-side):
    1. Z-order: after Typesetting.render_paragraph builds the final
       composition, raise every emitted original char (render_order None +
       sub_render_order 0 -- composition chars and flattened formula chars
       alike) to sub_render_order=9999999999999999, so it sorts with the
       translated text, after every mask. Masks, forms and curves keep their
       stock ordering, so the white mask still covers the baked-in English
       of the background image. (Stamping the masks with render_order=0 was
       tried first and regressed: masks then painted before the background
       image form/curves and the blue English bled through.)
    2. Glyph-less font substitution: some scan-OCR fonts (e.g. HiddenHorzOCR)
       embed a CFF with CharStrings count=1 -- literally no glyphs. Chars
       whose font_id points at such a font render as nothing even when
       painted above the mask (verified: p9 "153~" invisible in both stock
       and v1/v2 because C0_0 has 1 charstring, GIDs 49/51/53/126 out of
       range). For those chars, if the character code is ASCII (<=0x7E) we
       swap char.pdf_style.font_id to a real 1-byte font already on the page
       (Helvetica/Times, encoding_length=1) so the code renders correctly.

Known limitation (deliberately NOT handled):
    OCR sometimes misreads an icon shape as a letter (e.g. the p9 footprint
    icon yields a stray 'f' on font T1_0). Step 1 raises it above the mask,
    so it becomes visible. A drop-the-artifact pass was tried (detect the
    icon via its tall-narrow white mask) but removed: it risked deleting real
    text (a narrow multi-line block has the same mask shape) and the payoff
    is cosmetic only. A stray OCR glyph on an icon is accepted as harmless.

Scope:
    Only active when translation_config.ocr_workaround is true - digital PDFs
    keep stock behavior. Patches the in-memory class only; site-packages is
    never modified.

Switch:
    ME_FIX_RECT_ORDER=1 (default) enables both fixes, =0 disables.
"""

from __future__ import annotations

import os

_APPLIED = False
_TOP = 9999999999999999

# Cache: xref_id -> True if the font is glyph-less (CFF CharStrings count<=1).
# Populated lazily from the source PDF; avoids re-parsing per char.
_GLYPHLESS_CACHE: dict[int, bool] = {}


def _raise_original_chars(paragraph) -> int:
    """Stamp OCR-wiped original chars in a paragraph's compositions.

    Discriminator: render_order is None AND sub_render_order == 0 -- exactly
    what il_creater writes under ocr_workaround. Translated chars have
    sub_render_order None/absent and are left untouched.
    """
    stamped = 0
    for comp in paragraph.pdf_paragraph_composition or []:
        char = comp.pdf_character
        if (
            char is not None
            and getattr(char, "render_order", None) is None
            and getattr(char, "sub_render_order", None) == 0
        ):
            char.sub_render_order = _TOP
            stamped += 1
        formula = comp.pdf_formula
        if formula is not None:
            for fchar in formula.pdf_character or []:
                if (
                    getattr(fchar, "render_order", None) is None
                    and getattr(fchar, "sub_render_order", None) == 0
                ):
                    fchar.sub_render_order = _TOP
                    stamped += 1
    return stamped


def _font_glyph_count(font_bytes: bytes) -> int | None:
    """Return the font's glyph count via MuPDF, or None if unloadable.

    MuPDF handles CFF, TrueType and OTF uniformly; a stub scan-OCR font
    (e.g. HiddenHorzOCR) loads with glyph_count==1 (only .notdef).
    """
    try:
        import pymupdf

        return pymupdf.Font(fontbuffer=font_bytes).glyph_count
    except Exception:
        return None


def _is_glyphless_font(xref_id: int | None, source_path: str | None) -> bool:
    """Check (with cache) whether the font at xref_id is glyph-less."""
    if xref_id is None or not source_path:
        return False
    if xref_id in _GLYPHLESS_CACHE:
        return _GLYPHLESS_CACHE[xref_id]
    result = False
    try:
        import pymupdf

        doc = pymupdf.open(source_path)
        try:
            # extract_font returns (name, ext, subtype, buffer).
            font_bytes = doc.extract_font(xref_id)[3]
            if font_bytes:
                count = _font_glyph_count(font_bytes)
                # count<=1 means only .notdef (or empty): no usable glyphs.
                result = count is not None and count <= 1
        finally:
            doc.close()
    except Exception:
        result = False  # can't tell -> assume fine, don't substitute
    _GLYPHLESS_CACHE[xref_id] = result
    return result


def _find_substitute_font_id(page, orig_font_id: str) -> str | None:
    """Pick a real 1-byte font on the page to replace a glyph-less one.

    Prefers Helvetica/Times (base-14, always present in scan OCR PDFs);
    falls back to any font with encoding_length==1 that is not the
    glyph-less original. Returns None if no substitute found.
    """
    fonts = getattr(page, "pdf_font", None) or []
    candidates = []
    for f in fonts:
        fid = getattr(f, "font_id", None)
        if not fid or fid == orig_font_id:
            continue
        if getattr(f, "encoding_length", None) != 1:
            continue
        # Skip CID/Identity-H fonts (encoding_length would be 2 anyway).
        name = (getattr(f, "name", "") or "").lower()
        candidates.append((fid, name))
    if not candidates:
        return None
    # Prefer known-good base-14 names.
    for pref in ("helvetica", "times", "arial"):
        for fid, name in candidates:
            if pref in name:
                return fid
    # Fallback: first 1-byte candidate.
    return candidates[0][0]


def _substitute_glyphless_chars(paragraph, page, source_path: str | None) -> int:
    """Swap font_id of ASCII original chars whose font has no glyphs.

    Touches any original char (render_order is None -- the ocr_workaround
    wipe) whose font is glyph-less. Does NOT require sub_render_order==_TOP:
    some OCR chars (e.g. HiddenHorzOCR "153~" on p9) keep
    sub_render_order=None -- they already sort after masks like translated
    text, so z-order is fine; their font is simply glyph-less. Character
    code must be <=0x7E so it maps correctly under a 1-byte font.
    """
    substituted = 0
    debug = os.environ.get("ME_DEBUG_SUBST") == "1"
    # Build font_id -> xref_id map from page fonts.
    font_xref: dict[str, int | None] = {}
    for f in getattr(page, "pdf_font", None) or []:
        fid = getattr(f, "font_id", None)
        if fid:
            font_xref[fid] = getattr(f, "xref_id", None)
    if debug:
        pn = getattr(page, "page_number", "?")
        print(f"[me][dbg] page {pn} fonts={font_xref}")
    for comp in paragraph.pdf_paragraph_composition or []:
        char = comp.pdf_character
        if debug and char is not None:
            print(
                f"[me][dbg] raw ch={getattr(char,'char_unicode','?')!r} "
                f"code={getattr(char,'pdf_character_id',None)!r} "
                f"ro={getattr(char,'render_order',None)!r} "
                f"sro={getattr(char,'sub_render_order',None)!r} "
                f"fid={getattr(getattr(char,'pdf_style',None),'font_id',None)!r}"
            )
        if char is None:
            continue
        # Original chars only (ocr_workaround wipes render_order to None).
        # Do NOT require sub_render_order==_TOP: some OCR chars (e.g. the
        # HiddenHorzOCR "153~" on p9) keep sub_render_order=None -- they
        # already sort after masks like translated text, so z-order is fine;
        # their font is simply glyph-less. Only the font needs fixing.
        if getattr(char, "render_order", None) is not None:
            continue
        code = getattr(char, "pdf_character_id", None)
        if code is None or code > 0x7E:
            if debug:
                print(f"[me][dbg] skip code={code!r}")
            continue
        style = getattr(char, "pdf_style", None)
        if style is None:
            continue
        orig_fid = getattr(style, "font_id", None)
        if not orig_fid:
            continue
        xref = font_xref.get(orig_fid)
        if debug:
            print(
                f"[me][dbg] cand code={code:#x} ch={getattr(char,'char_unicode','?')!r} "
                f"fid={orig_fid} xref={xref}"
            )
        if not _is_glyphless_font(xref, source_path):
            continue
        sub_fid = _find_substitute_font_id(page, orig_fid)
        if debug:
            print(f"[me][dbg]   glyphless=True sub={sub_fid}")
        if sub_fid is None:
            continue
        style.font_id = sub_fid
        substituted += 1
    return substituted


def apply() -> bool:
    """Install the patch. Returns True if the patch is active after the call.

    Idempotent: safe to call multiple times in one process.
    """
    global _APPLIED
    if os.environ.get("ME_FIX_RECT_ORDER", "1") == "0":
        return False
    if _APPLIED:
        return True

    try:
        from babeldoc.format.pdf.document_il.midend import typesetting
    except Exception as exc:  # babeldoc layout drift - degrade to stock behavior
        print(f"[me] ocr rect-order patch: DISABLED (babeldoc import failed: {exc})")
        return False

    original = typesetting.Typesetting.render_paragraph
    if getattr(original, "_me_rect_order_patch", False):
        _APPLIED = True
        return True

    def patched(self, paragraph, page, fonts):
        result = original(self, paragraph, page, fonts)
        config = getattr(self, "translation_config", None)
        if config is not None and getattr(config, "ocr_workaround", False):
            _raise_original_chars(paragraph)
            source_path = str(getattr(config, "input_file", "") or "")
            n = _substitute_glyphless_chars(paragraph, page, source_path)
            if n:
                print(f"[me] glyphless font substitution: {n} chars on page")
        return result

    patched._me_rect_order_patch = True
    typesetting.Typesetting.render_paragraph = patched
    _APPLIED = True
    return True
