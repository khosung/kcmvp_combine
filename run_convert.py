"""kcmvp_combine: docling Fix17 + post-processing 통합 변환기

우선순위별 후처리(post_process.py)를 docling 변환 직후 적용한다:
  Priority 1 : FORMULA 레이아웃 요소 → $$...$$ 블록 출력 복원
  Priority 1b: FORMULA LaTeX 공백 정규화
  Priority 2 : 암호 도메인 수식 패턴 → $...$ 인라인 래핑
  Priority 3 : 표 셀 수식 패턴 → $...$ 래핑
  Priority 4 : 수식 조각 HEADING/TEXT 억제
  Priority 5 : 텍스트 NFC + 하이픈 줄바꿈 복원 + 전각→반각

청크 변환:
  총 페이지 수가 CHUNK_THRESHOLD(기본 15) 를 초과하면 CHUNK_SIZE(기본 3) 페이지씩
  나누어 변환 후 마크다운을 결합한다.  PyMuPDF(fitz) 또는 pdfplumber 가 필요하다.
  없으면 단일 패스로 변환한다.

사전 조건:
  - `pip install -r requirements.txt` 로 docling 을 설치한 뒤,
  - 최초 실행 시 Fix17 패치가 자동으로 설치된 docling 파일에 적용된다.

사용법:
    python run_convert.py <input_pdf> <output_dir>
"""

import logging
import re
import sys
from pathlib import Path

# ── 로깅 설정: docling 내부 basicConfig 에 덮이지 않도록 force=True
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    datefmt="%H:%M:%S",
    force=True,
)

# ── Fix17 패치 자동 적용 ── docling import 전에 실행해야 한다 ─────────────────
from apply_patches import apply as _apply_fix17_patches
_apply_fix17_patches()  # 이미 패치됐으면 아무 작업도 하지 않는다
del _apply_fix17_patches

from docling.datamodel.base_models import InputFormat
from docling.datamodel.pipeline_options import PdfPipelineOptions
from docling.document_converter import DocumentConverter, PdfFormatOption
from docling_core.types.doc import ImageRefMode

# post_process.py 는 같은 디렉터리에 있어야 한다
sys.path.insert(0, str(Path(__file__).parent))
from post_process import (
    apply_all,
    apply_md_cleanup,
    extract_figure_captions,
    inject_figure_captions,
    _is_formula_fragment,
)

# ── 청크 변환 설정 ────────────────────────────────────────────────────────────
CHUNK_SIZE = 3        # 한 번에 처리할 페이지 수
CHUNK_THRESHOLD = 15  # 이 값을 초과하면 청크 변환 사용


def _get_page_count(pdf_path: Path) -> int:
    """PDF 총 페이지 수를 반환한다. 라이브러리 없으면 -1."""
    try:
        import fitz  # PyMuPDF
        with fitz.open(str(pdf_path)) as doc:
            return doc.page_count
    except ImportError:
        pass
    try:
        import pdfplumber
        with pdfplumber.open(str(pdf_path)) as doc:
            return len(doc.pages)
    except Exception:
        return -1


def _convert_chunked(
    converter: DocumentConverter,
    input_pdf: Path,
    chunk_size: int = CHUNK_SIZE,
) -> tuple[str | None, list]:
    """PDF 를 chunk_size 페이지 단위로 변환해 (마크다운 문자열, 캡션 목록) 을 반환한다.

    페이지 수를 알 수 없거나 청크 변환에 실패하면 (None, []) 반환 → 호출자가 단일 패스로 폴백.
    """
    total = _get_page_count(input_pdf)
    if total <= 0:
        return None, []

    md_parts: list[str] = []
    all_captions: list = []
    for start in range(1, total + 1, chunk_size):
        end = min(start + chunk_size - 1, total)
        print(f"  청크 변환: {start}~{end} / {total}")
        result = converter.convert(input_pdf, page_range=(start, end))
        apply_all(result.document, verbose=False)
        all_captions.extend(extract_figure_captions(result.document))
        md_parts.append(
            result.document.export_to_markdown(image_mode=ImageRefMode.REFERENCED)
        )

    return "\n\n".join(md_parts) if md_parts else None, all_captions


_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+)$")


def _suppress_heading_fragments(md: str) -> str:
    """마크다운에서 수식 조각 헤딩 라인을 제거한다.

    단일 변수(X), 순수 숫자(127) 등 짧은 수식 조각이 HEADING 으로 오분류된 경우
    해당 줄을 제거하여 heading 카운트 과다를 방지한다.
    """
    lines = md.split("\n")
    result: list[str] = []
    for line in lines:
        m = _HEADING_RE.match(line)
        if m and _is_formula_fragment(m.group(2).strip()):
            continue  # 수식 조각 헤딩 제거
        result.append(line)
    return "\n".join(result)


def main() -> None:
    if len(sys.argv) != 3:
        print("Usage: python run_convert.py <input_pdf> <output_dir>")
        sys.exit(1)

    input_pdf = Path(sys.argv[1])
    output_dir = Path(sys.argv[2])

    if not input_pdf.exists():
        print(f"Error: file not found: {input_pdf}")
        sys.exit(1)

    output_dir.mkdir(parents=True, exist_ok=True)

    pipeline_options = PdfPipelineOptions(
        do_ocr=False,
        images_scale=1.0,
        generate_page_images=False,
        generate_picture_images=True,
        generate_table_images=True,
    )

    converter = DocumentConverter(
        format_options={
            InputFormat.PDF: PdfFormatOption(pipeline_options=pipeline_options)
        }
    )

    stem = input_pdf.stem
    out_md = output_dir / f"{stem}.md"

    total_pages = _get_page_count(input_pdf)
    print(f"Converting: {input_pdf}  ({total_pages if total_pages > 0 else '?'} pages)")

    if total_pages > CHUNK_THRESHOLD:
        print(f"  총 {total_pages}페이지 → {CHUNK_SIZE}페이지 단위 청크 변환")
        md_content, fig_captions = _convert_chunked(converter, input_pdf, CHUNK_SIZE)
        if md_content is None:
            print("  청크 변환 실패, 단일 패스로 폴백")
            result = converter.convert(input_pdf)
            apply_all(result.document, verbose=True)
            fig_captions = extract_figure_captions(result.document)
            md_content = result.document.export_to_markdown(image_mode=ImageRefMode.REFERENCED)
        md_content, n_fig = inject_figure_captions(md_content, fig_captions)
        print(f"[figure_caption] 그림 캡션 {n_fig}개 삽입")
        md_content, md_stats = apply_md_cleanup(md_content, verbose=True)
        out_md.write_text(md_content, encoding="utf-8")
    else:
        result = converter.convert(input_pdf)
        stats = apply_all(result.document, verbose=True)
        total = sum(stats.values())
        print(f"[post_process] 총 {total} 항목/셀 개선 적용")
        fig_captions = extract_figure_captions(result.document)
        result.document.save_as_markdown(filename=out_md, image_mode=ImageRefMode.REFERENCED)
        # 저장된 파일을 읽어 그림 캡션 + 마크다운 레벨 후처리 적용
        md_text = out_md.read_text(encoding="utf-8")
        md_text, n_fig = inject_figure_captions(md_text, fig_captions)
        print(f"[figure_caption] 그림 캡션 {n_fig}개 삽입")
        md_text, md_stats = apply_md_cleanup(md_text, verbose=True)
        out_md.write_text(md_text, encoding="utf-8")

    print(f"Saved: {out_md}")


if __name__ == "__main__":
    main()
