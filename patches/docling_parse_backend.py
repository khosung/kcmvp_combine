# Fix17-patched
import logging
import re
from collections.abc import Iterable
from io import BytesIO
from pathlib import Path
from typing import TYPE_CHECKING, Optional, Union

import pypdfium2 as pdfium
from docling_core.types.doc import BoundingBox, CoordOrigin
from docling_core.types.doc.page import SegmentedPdfPage, TextCell
from docling_parse.pdf_parser import DoclingPdfParser, PdfDocument
from docling_parse.pdf_parsers import DecodePageConfig
from PIL import Image
from pypdfium2 import PdfPage

from docling.backend.pdf_backend import PdfDocumentBackend, PdfPageBackend
from docling.datamodel.backend_options import PdfBackendOptions
from docling.datamodel.base_models import Size
from docling.utils.locks import pypdfium2_lock

if TYPE_CHECKING:
    from docling.datamodel.document import InputDocument

_log = logging.getLogger(__name__)

# Minimum advance width (in PDF user-space points) of an empty-text glyph to be
# treated as a word-boundary space.  Korean PDFs commonly encode inter-word spaces
# this way: the glyph has no Unicode mapping (text='') but a non-zero width that
# visually separates adjacent words.
_KOREAN_SPACE_WIDTH_THRESHOLD = 1.5

# ── HyhwpEQ PUA decoding ─────────────────────────────────────────────────────
# Korean HWP equation editor embeds math symbols using Private Use Area (PUA)
# codepoints in the /INPILL+HyhwpEQ font.  The mapping below was reverse-
# engineered from PDF inspection and context analysis.
#
# Systematic rules
#   Uppercase A-Z : PUA = 0xE000 + (ord(ch) - 0x41)  →  E000…E019
#   Lowercase a-z : PUA = 0xE084 + ord(ch)            →  E0E5…E0FE
#
# Known special codepoints (empirically confirmed from document context;
# extended with full kcmvp_rag _HYHWPEQ_PUA_MAP for better crypto-doc coverage)
_HWPEQ_SPECIAL: dict[int, str] = {
    # Digits 0-9  (0xE033 is a secondary zero glyph; primary is 0xE03D)
    0xE033: "0",
    0xE034: "1",
    0xE035: "2",
    0xE036: "3",
    0xE037: "4",
    0xE038: "5",
    0xE039: "6",
    0xE03A: "7",
    0xE03B: "8",
    0xE03C: "9",
    # Common operators
    # 0xE03D is '0' (not '='): confirmed from CT[0]=IV context
    0xE03D: "0",
    0xE03E: ".",   # 마침표 (중간점)
    0xE044: "(",
    0xE045: ")",
    0xE046: "-",   # 빼기/마이너스 (confirmed; kcmvp_rag maps as '_' but context confirms '-')
    # 0xE047 is '=' (assignment/equality): confirmed from Y=ENC(X,Key) context
    0xE047: "=",
    0xE048: "+",
    0xE049: "[",
    0xE04A: "]",
    0xE04B: "{",
    0xE04C: "}",
    0xE04D: r"\parallel",  # ∥ in LaTeX (confirmed from document)
    0xE04E: "!",   # 느낌표
    0xE04F: ":",   # 콜론
    0xE050: "-",   # 빼기 연산자 (0xE046 과 구분)
    0xE051: "·",   # 가운뎃점 (곱셈점, U+00B7)
    0xE052: ",",
    0xE053: ".",   # confirmed from document
    0xE054: "==",  # 동치 (confirmed from document; kcmvp_rag maps as '/')
    0xE055: "<",
    0xE056: ">",
    # Comparison / relational operators
    0xE057: "≤",   # ≤ (U+2264)
    0xE058: "≥",   # ≥ (U+2265)
    0xE059: "≠",   # ≠ (U+2260)
    0xE05A: "≈",   # ≈ (U+2248)
    0xE05B: "∞",   # ∞ (U+221E)
    0xE05C: "√",   # √ (U+221A)
    # Arrows
    0xE05D: "←",   # ← (U+2190)
    0xE05E: "→",   # → (U+2192)
    0xE05F: "↑",   # ↑ (U+2191)
    0xE060: "↓",   # ↓ (U+2193)
    # Logic / Set operators
    0xE061: "⊕",   # ⊕ XOR (U+2295)
    0xE062: "⊗",   # ⊗ (U+2297)
    0xE063: "∈",   # ∈ (U+2208)
    0xE064: "∉",   # ∉ (U+2209)
    0xE065: "⊂",   # ⊂ (U+2282)
    0xE066: "⊃",   # ⊃ (U+2283)
    0xE067: "∪",   # ∪ (U+222A)
    0xE068: "∩",   # ∩ (U+2229)
    0xE069: "∧",   # ∧ (U+2227)
    0xE06A: "∨",   # ∨ (U+2228)
    0xE06B: "¬",   # ¬ (U+00AC)
    0xE06C: "∥",   # ∥ 평행/연결 (U+2225)
    0xE06D: "_",   # 밑첨자 마커
    # Arithmetic
    0xE06E: "×",   # × (U+00D7)
    0xE06F: "÷",   # ÷ (U+00F7)
    0xE070: "±",   # ± (U+00B1)
    # Big operators
    0xE071: "∑",   # ∑ (U+2211)
    0xE072: "∏",   # ∏ (U+220F)
    0xE073: "∫",   # ∫ (U+222B)
    # Shift operators
    0xE074: "≪",   # ≪ (U+226A)
    0xE075: "≫",   # ≫ (U+226B)
    # Superscript marker
    0xE0E2: "^",
    # Greek letters (lowercase) — critical for crypto documents
    0xE09B: "α",   # α (U+03B1)
    0xE09C: "β",   # β (U+03B2)
    0xE09D: "γ",   # γ (U+03B3)
    0xE09E: "δ",   # δ (U+03B4)
    0xE09F: "ε",   # ε (U+03B5)
    0xE0A0: "ζ",   # ζ (U+03B6)
    0xE0A1: "η",   # η (U+03B7)
    0xE0A2: "θ",   # θ (U+03B8)
    0xE0A3: "ι",   # ι (U+03B9)
    0xE0A4: "κ",   # κ (U+03BA)
    0xE0A5: "μ",   # μ (U+03BC)
    0xE0A6: "ν",   # ν (U+03BD)
    0xE0A7: "λ",   # λ (U+03BB) — 암호 문서에서 자주 사용
    0xE0A8: "ξ",   # ξ (U+03BE)
    0xE0A9: "π",   # π (U+03C0)
    0xE0AA: "ρ",   # ρ (U+03C1)
    0xE0AB: "σ",   # σ (U+03C3)
    0xE0AC: "τ",   # τ (U+03C4)
    0xE0AD: "υ",   # υ (U+03C5)
    0xE0AE: "φ",   # φ (U+03C6)
    0xE0AF: "χ",   # χ (U+03C7)
    0xE0B0: "ψ",   # ψ (U+03C8)
    0xE0B1: "ω",   # ω (U+03C9)
}


def _decode_hwpeq_char(ch: str) -> str:
    """Return the readable equivalent of a single HyhwpEQ PUA character.

    Falls back to the original character for unmapped codepoints so that
    content is never silently dropped.
    """
    if not ch:
        return ""
    code = ord(ch)
    if 0xE000 <= code <= 0xE019:          # uppercase A-Z
        return chr(code - 0xE000 + 0x41)
    if 0xE0E5 <= code <= 0xE0FE:          # lowercase a-z
        return chr(code - 0xE084)
    return _HWPEQ_SPECIAL.get(code, ch)   # special or unknown (keep original)


def _restore_korean_spaces(
    textline_cells: list,
    char_cells: list,
    space_threshold: float = _KOREAN_SPACE_WIDTH_THRESHOLD,
    bbox_tol: float = 1.0,
) -> None:
    """Re-insert word-boundary spaces and decode HyhwpEQ inline math.

    For each textline cell the function:
    1. Collects all char cells whose centre lies inside the textline bbox.
    2. Sorts them by x-coordinate.
    3. Replaces every ``text=''`` char (from non-HyhwpEQ fonts) whose advance
       width is ≥ *space_threshold* with a single ASCII space.
    4. Wraps consecutive HyhwpEQ-font chars in LaTeX ``$...$`` after decoding
       each PUA codepoint via :func:`_decode_hwpeq_char`.
    5. Writes the reconstructed string back to ``textline_cell.text``.

    Cells whose chars cannot be located (e.g. vector-only glyphs) are left
    unchanged so the existing text is used as a fallback.
    """
    for tl in textline_cells:
        tl_left = min(tl.rect.r_x0, tl.rect.r_x3)
        tl_right = max(tl.rect.r_x1, tl.rect.r_x2)
        # After to_top_left_origin, y increases downward.
        # r_y0/r_y1 hold the smaller (top) value and r_y2/r_y3 the larger (bottom).
        # Use min/max across all four corners to be safe regardless of rotation.
        tl_top = min(tl.rect.r_y0, tl.rect.r_y1, tl.rect.r_y2, tl.rect.r_y3)
        tl_bot = max(tl.rect.r_y0, tl.rect.r_y1, tl.rect.r_y2, tl.rect.r_y3)

        chars = [
            c
            for c in char_cells
            if (
                tl_left - bbox_tol
                <= (c.rect.r_x0 + c.rect.r_x1) / 2
                <= tl_right + bbox_tol
                and tl_top - bbox_tol
                <= (c.rect.r_y0 + c.rect.r_y2) / 2
                <= tl_bot + bbox_tol
            )
        ]

        if not chars:
            continue  # no chars matched — leave original text untouched

        chars.sort(key=lambda c: c.rect.r_x0)

        # ── rebuild text with space restoration + HyhwpEQ math conversion ──
        result_parts: list[str] = []
        math_run: list[str] = []
        in_math_run = False

        for i, c in enumerate(chars):
            is_hwpeq = "HyhwpEQ" in (c.font_name or "")
            char_width = abs(c.rect.r_x1 - c.rect.r_x0)

            if is_hwpeq:
                # Word-boundary space glyphs within HyhwpEQ are PDF rendering
                # artefacts; skip them so intra-formula gaps don't fragment the run.
                if c.text == "" and char_width >= space_threshold:
                    continue
                decoded = _decode_hwpeq_char(c.text) if c.text else ""
                if not in_math_run:
                    in_math_run = True
                    math_run = [decoded] if decoded else []
                else:
                    if decoded:
                        math_run.append(decoded)
            else:
                # Non-HyhwpEQ word-boundary space between two HyhwpEQ segments:
                # peek ahead — if the next char is also HyhwpEQ this space is an
                # intra-formula artefact and we skip it, keeping the math run open.
                if (
                    in_math_run
                    and c.text == ""
                    and char_width >= space_threshold
                    and i + 1 < len(chars)
                    and "HyhwpEQ" in (chars[i + 1].font_name or "")
                ):
                    continue

                # Flush any open math run before processing regular text.
                if in_math_run:
                    math_str = "".join(math_run).strip()
                    for _cmd in (r"\cdots", r"\oplus", r"\le", r"\ge", r"\parallel"):
                        repl = _cmd + " "
                        math_str = re.sub(re.escape(_cmd) + r"(?=[A-Za-z0-9])", lambda m, r=repl: r, math_str)
                    if math_str:
                        result_parts.append(f"${math_str}$")
                    in_math_run = False
                    math_run = []

                # Korean word-boundary space restoration.
                if c.text == "" and char_width >= space_threshold:
                    result_parts.append(" ")
                else:
                    result_parts.append(c.text)

        # Flush a trailing math run.
        if in_math_run and math_run:
            math_str = "".join(math_run).strip()
            for _cmd in (r"\cdots", r"\oplus", r"\le", r"\ge", r"\parallel"):
                repl = _cmd + " "
                math_str = re.sub(re.escape(_cmd) + r"(?=[A-Za-z0-9])", lambda m, r=repl: r, math_str)
            if math_str:
                result_parts.append(f"${math_str}$")

        result = "".join(result_parts).strip()

        if result:
            tl.text = result


def _decode_hwpeq_in_textlines(textline_cells: list) -> None:
    """Fallback: convert any remaining HyhwpEQ PUA chars directly from textline text.

    Operates purely on the text string of each textline cell, so it works even
    when :func:`_restore_korean_spaces` did not find matching char cells for a
    particular textline.  Consecutive PUA codepoints (0xE000–0xF8FF) are decoded
    and wrapped in ``$...$``; a lone ASCII space that is *surrounded* by PUA chars
    on both sides is kept inside the math run.
    """
    for tl in textline_cells:
        text = tl.text
        if not text:
            continue
        # Fast check: skip if no PUA chars at all.
        if not any(0xE000 <= ord(c) <= 0xF8FF for c in text):
            continue

        result: list[str] = []
        i = 0
        while i < len(text):
            ch = text[i]
            code = ord(ch)
            if 0xE000 <= code <= 0xF8FF:
                # Start of a math run: consume PUA chars (and embedded spaces).
                math_chars: list[str] = []
                while i < len(text):
                    ch = text[i]
                    code = ord(ch)
                    if 0xE000 <= code <= 0xF8FF:
                        math_chars.append(_decode_hwpeq_char(ch))
                        i += 1
                    elif (
                        ch == " "
                        and i + 1 < len(text)
                        and 0xE000 <= ord(text[i + 1]) <= 0xF8FF
                    ):
                        # Space between PUA chars — keep it inside the math run.
                        math_chars.append(" ")
                        i += 1
                    else:
                        break
                math_str = "".join(math_chars).strip()
                if math_str:
                    result.append(f"${math_str}$")
            else:
                result.append(ch)
                i += 1

        new_text = "".join(result).strip()
        if new_text and new_text != text:
            tl.text = new_text


class DoclingParsePageBackend(PdfPageBackend):
    def __init__(
        self,
        *,
        dp_doc: PdfDocument,
        page_obj: PdfPage,
        page_no: int,
        create_words: bool = True,
        create_textlines: bool = True,
        keep_chars: bool = False,
        keep_lines: bool = False,
        keep_images: bool = True,
    ):
        self._ppage = page_obj
        self._dp_doc = dp_doc
        self._page_no = page_no

        self._create_words = create_words
        self._create_textlines = create_textlines

        self._keep_chars = keep_chars
        self._keep_lines = keep_lines
        self._keep_images = keep_images

        self._dpage: Optional[SegmentedPdfPage] = None
        self._unloaded = False
        self.valid = (self._ppage is not None) and (self._dp_doc is not None)

    def _ensure_parsed(self) -> None:
        if self._dpage is not None:
            return

        # FIXME for the future: we will want to make this config a
        # member of the class, i.e. self.config. Ultimately, we also
        # should not need to keep the char's, but it seems no lines
        # get created if we dont keep the chars. Updated version of
        # docling-parse >v5.3.0 should fix this.
        config = DecodePageConfig()
        config.keep_char_cells = (
            True  # we need to set this to True, otherwhise we have no lines
        )
        config.keep_shapes = False  # we dont need this, self._keep_lines
        config.keep_bitmaps = (
            True  # we need to set this to True, otherwhise OCR will not work
        )
        config.create_word_cells = self._create_words
        config.create_line_cells = self._create_textlines
        config.enforce_same_font = True

        seg_page = self._dp_doc.get_page(self._page_no + 1, config=config)

        # In Docling, all TextCell instances are expected with top-left origin.
        [
            tc.to_top_left_origin(seg_page.dimension.height)
            for tc in seg_page.textline_cells
        ]
        [tc.to_top_left_origin(seg_page.dimension.height) for tc in seg_page.char_cells]
        [tc.to_top_left_origin(seg_page.dimension.height) for tc in seg_page.word_cells]

        # Restore word-boundary spaces that Korean PDFs encode as empty glyphs.
        # PDF fonts often represent inter-word spaces as a glyph with text=''
        # but non-zero advance width; docling-parse drops these, causing all
        # Korean words to be joined without spaces.
        _restore_korean_spaces(seg_page.textline_cells, seg_page.char_cells)
        # Also process word-level cells (used by table extraction).
        # word_cells holds smaller text fragments than textline_cells; here we
        # only decode PUA chars but let the table pipeline assemble $...$ from
        # the textline layer when possible.
        _restore_korean_spaces(seg_page.word_cells, seg_page.char_cells)

        self._dpage = seg_page

    def is_valid(self) -> bool:
        return self.valid

    def get_text_in_rect(self, bbox: BoundingBox) -> str:
        self._ensure_parsed()
        assert self._dpage is not None

        # Find intersecting cells on the page
        text_piece = ""
        page_size = self.get_size()

        scale = (
            1  # FIX - Replace with param in get_text_in_rect across backends (optional)
        )

        for i, cell in enumerate(self._dpage.textline_cells):
            cell_bbox = (
                cell.rect.to_bounding_box()
                .to_top_left_origin(page_height=page_size.height)
                .scaled(scale)
            )

            overlap_frac = cell_bbox.intersection_over_self(bbox)

            if overlap_frac > 0.5:
                if len(text_piece) > 0:
                    text_piece += " "
                text_piece += cell.text

        return text_piece

    def get_segmented_page(self) -> Optional[SegmentedPdfPage]:
        self._ensure_parsed()
        return self._dpage

    def get_text_cells(self) -> Iterable[TextCell]:
        self._ensure_parsed()
        assert self._dpage is not None

        return self._dpage.textline_cells

    def get_bitmap_rects(self, scale: float = 1) -> Iterable[BoundingBox]:
        self._ensure_parsed()
        assert self._dpage is not None

        AREA_THRESHOLD = 0  # 32 * 32

        images = self._dpage.bitmap_resources

        for img in images:
            cropbox = img.rect.to_bounding_box().to_top_left_origin(
                self.get_size().height
            )

            if cropbox.area() > AREA_THRESHOLD:
                cropbox = cropbox.scaled(scale=scale)

                yield cropbox

    def get_page_image(
        self, scale: float = 1, cropbox: Optional[BoundingBox] = None
    ) -> Image.Image:
        page_size = self.get_size()

        if not cropbox:
            cropbox = BoundingBox(
                l=0,
                r=page_size.width,
                t=0,
                b=page_size.height,
                coord_origin=CoordOrigin.TOPLEFT,
            )
            padbox = BoundingBox(
                l=0, r=0, t=0, b=0, coord_origin=CoordOrigin.BOTTOMLEFT
            )
        else:
            padbox = cropbox.to_bottom_left_origin(page_size.height).model_copy()
            padbox.r = page_size.width - padbox.r
            padbox.t = page_size.height - padbox.t

        with pypdfium2_lock:
            image = (
                self._ppage.render(
                    scale=scale * 1.5,
                    rotation=0,  # no additional rotation
                    crop=padbox.as_tuple(),
                )
                .to_pil()
                .resize(
                    size=(round(cropbox.width * scale), round(cropbox.height * scale))
                )
            )  # We resize the image from 1.5x the given scale to make it sharper.

        return image

    def get_size(self) -> Size:
        with pypdfium2_lock:
            return Size(width=self._ppage.get_width(), height=self._ppage.get_height())

        # TODO: Take width and height from docling-parse.
        # return Size(
        #    width=self._dpage.dimension.width,
        #    height=self._dpage.dimension.height,
        # )

    def unload(self):
        if not self._unloaded and self._dp_doc is not None:
            self._dp_doc.unload_pages((self._page_no + 1, self._page_no + 2))
            self._unloaded = True

        self._ppage = None
        self._dpage = None
        self._dp_doc = None


class DoclingParseDocumentBackend(PdfDocumentBackend):
    def __init__(
        self,
        in_doc: "InputDocument",
        path_or_stream: Union[BytesIO, Path],
        options: PdfBackendOptions = PdfBackendOptions(),
    ):
        super().__init__(in_doc, path_or_stream, options)

        password = (
            self.options.password.get_secret_value() if self.options.password else None
        )
        with pypdfium2_lock:
            self._pdoc = pdfium.PdfDocument(self.path_or_stream, password=password)
        self.parser = DoclingPdfParser(loglevel="fatal")

        self.dp_doc: PdfDocument = self.parser.load(
            path_or_stream=self.path_or_stream, password=password
        )
        success = self.dp_doc is not None

        if not success:
            raise RuntimeError(
                f"docling-parse could not load document {self.document_hash}."
            )

    def page_count(self) -> int:
        # return len(self._pdoc)  # To be replaced with docling-parse API

        len_1 = len(self._pdoc)
        len_2 = self.dp_doc.number_of_pages()

        if len_1 != len_2:
            _log.error(f"Inconsistent number of pages: {len_1}!={len_2}")

        return len_2

    def load_page(
        self, page_no: int, create_words: bool = True, create_textlines: bool = True
    ) -> DoclingParsePageBackend:
        with pypdfium2_lock:
            ppage = self._pdoc[page_no]

        return DoclingParsePageBackend(
            dp_doc=self.dp_doc,
            page_obj=ppage,
            page_no=page_no,
            create_words=create_words,
            create_textlines=create_textlines,
        )

    def is_valid(self) -> bool:
        return self.page_count() > 0

    def unload(self):
        super().unload()
        # Unload docling-parse document first
        if self.dp_doc is not None:
            self.dp_doc.unload()
            self.dp_doc = None

        # Then close pypdfium2 document with proper locking
        if self._pdoc is not None:
            with pypdfium2_lock:
                try:
                    self._pdoc.close()
                except Exception:
                    # Ignore cleanup errors
                    pass
            self._pdoc = None
