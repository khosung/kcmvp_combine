"""Post-processing pass applied to a DoclingDocument after conversion.

우선순위별 후처리 적용:
  Priority 1 (fix_formula_items):
      Docling FORMULA 요소의 빈 text 를 orig 기반으로 복원.
  Priority 1b (normalize_formula_whitespace_in_doc):
      FORMULA 아이템 LaTeX 공백 정규화 (다중 공백 → 단일, 중괄호 안 공백 제거).
  Priority 2 (apply_crypto_patterns):
      TEXT 계열 요소 암호 도메인 수식 패턴 → $...$ 래핑.
  Priority 3 (apply_table_cell_patterns):
      표 셀 수식 패턴 → $...$ 래핑.
  Priority 4 (suppress_formula_fragments):
      HEADING/TEXT 중 수식 조각(단일 변수, 숫자 등)으로 판별된 항목 억제.
  Priority 5 (normalize_text_items):
      텍스트 아이템 NFC 정규화 + 하이픈 줄바꿈 복원 + 전각→반각.

사용법:
    from post_process import apply_all
    stats = apply_all(result.document, verbose=True)
"""

from __future__ import annotations

import re
import unicodedata
from typing import TYPE_CHECKING

from docling_core.types.doc import DocItemLabel, DoclingDocument, TableItem, TextItem

try:
    from docling_core.types.doc import FormulaItem  # docling-core >= 2.x
except ImportError:
    FormulaItem = None  # type: ignore[assignment,misc]

# ─────────────────────────────────────────────────────────────────────────────
# 상수 및 정규식
# ─────────────────────────────────────────────────────────────────────────────

# Priority 2: 암호 도메인 수식 패턴 (kcmvp_rag FormulaExtractor.CRYPTO_FORMULA_PATTERN 기반)
_CRYPTO_FORMULA_PATTERN = re.compile(
    r"[A-Z][A-Za-z_]*\([^)]+\)\s*=|"           # ENC(X,Key) =
    r"=\s*[A-Z][A-Za-z_]*\([^)]+\)|"            # = Func(x)
    r"\([A-Z][a-z]*\s*,\s*[A-Z][a-z]*\)\s*=|"  # (Key, V) =
    r"\bmod\s+[0-9A-Za-zλ]|"                    # mod N
    r"\d+\^[\d{]|"                              # 2^{n}, 2^128
    r"\w+\^\{[^}]+\}|"                          # e^{-1}
    r"[A-Z][A-Za-z]*_\{?[A-Za-z0-9]+\}?\(|"    # MSB_{i}(
    r"∥|⊕|"                                     # 연결, XOR
    r"√|"                                        # 제곱근
    r"\b(?:LCM|GCD|lcm|gcd)\(|"                 # LCM(, GCD(
    r"\b(?:ENC|DEC|Hash|HMAC|Update|Reseed|GenPrime|GenRand|GF|Mul_H)\(|"
    r"\b[A-Z][a-z]*\[\w+\]\s*=|"               # CT[i] =
    r"=\s*[A-Z][a-z]*\[\w+\]|"                 # = PT[i]
    r"\bfor\s+\w+\s+from\s+\d+\s+to\s+\d+"     # for i from 0 to 127
)

# Priority 3: 표 셀 수식 패턴 (kcmvp_rag FormulaExtractor.TABLE_CELL_FORMULA_PATTERN 기반)
_TABLE_CELL_FORMULA_PATTERN = re.compile(
    r"[A-Za-z]\w*_\{?\w+\}?|"           # 첨자 변수: x_0, Z_{i+1}
    r"\d+\^[\d{]|"                       # 거듭제곱: 2^128
    r"[A-Za-z]\w*\s*←\s*[A-Za-z]|"      # 화살표 대입: Z ← X
    r"\(if\s+|"                          # 조건식
    r"\bfor\s+\w+.*\bto\s+\d+|"         # 루프
    r"≫|≪|>>|<<|"                       # 시프트
    r"\bLSB\w*\(|\bMSB\w*\(|\bGF\(|"    # 암호 함수
    r"0\^[\d{]|1\^[\d{]|"               # 비트열
    r"\w+\s*⊕\s*\w+|"                   # XOR: Z_i ⊕ V_i
    r"\w+\s*∥\s*\w+"                    # 연결: A ∥ B
)

# 이미 $...$ 로 감싸인 토큰을 제외하기 위한 패턴
_ALREADY_DOLLAR = re.compile(r"\$[^$]+\$")

# ─────────────────────────────────────────────────────────────────────────────
# 내부 헬퍼
# ─────────────────────────────────────────────────────────────────────────────

def _strip_dollar_delimiters(text: str) -> str:
    """$$...$$ 또는 $...$ 래핑을 한 겹 제거한다."""
    t = text.strip()
    if t.startswith("$$") and t.endswith("$$") and len(t) > 4:
        return t[2:-2].strip()
    if t.startswith("$") and t.endswith("$") and len(t) > 2:
        return t[1:-1].strip()
    return t


def _wrap_crypto_spans(text: str) -> str:
    """CRYPTO_FORMULA_PATTERN 에 매칭되는 토큰을 $...$ 로 래핑한다.

    이미 $...$ 안에 있는 구간은 건너뛴다.
    패턴이 감지되지 않으면 원문을 그대로 반환한다.
    """
    if not _CRYPTO_FORMULA_PATTERN.search(text):
        return text  # 빠른 반환

    # $...$ 로 이미 감싸인 구간의 인덱스 집합 구축
    protected: set[int] = set()
    for m in _ALREADY_DOLLAR.finditer(text):
        protected.update(range(m.start(), m.end()))

    result: list[str] = []
    pos = 0
    for m in _CRYPTO_FORMULA_PATTERN.finditer(text):
        s, e = m.start(), m.end()
        # 보호 구간과 겹치면 건너뜀
        if protected.intersection(range(s, e)):
            continue
        result.append(text[pos:s])
        result.append(f"${m.group()}$")
        pos = e
    result.append(text[pos:])
    return "".join(result)


# ─────────────────────────────────────────────────────────────────────────────
# Priority 1
# ─────────────────────────────────────────────────────────────────────────────

def fix_formula_items(doc: DoclingDocument) -> int:
    """FORMULA 아이템의 빈 text 를 orig 기반으로 복원한다.

    docling 은 layout 모델이 FORMULA 로 감지한 영역을 text="" / orig=<raw> 로
    저장한다. text 가 비어 있으면 markdown 내보내기 시 <!-- formula-not-decoded -->
    로 출력되므로, orig 값을 text 에 설정해 $$...$$ 블록으로 출력되게 한다.

    Returns:
        복원된 FORMULA 아이템 수
    """
    count = 0
    for item in doc.texts:
        # FormulaItem 이 import 된 경우 isinstance 체크, 아니면 label 로 확인
        is_formula = (
            (FormulaItem is not None and isinstance(item, FormulaItem))
            or item.label == DocItemLabel.FORMULA
        )
        if not is_formula:
            continue
        if item.text:
            continue  # 이미 채워져 있음 (CodeFormula enrichment 등)
        if not item.orig:
            continue

        raw = item.orig.strip()
        decoded = _strip_dollar_delimiters(raw)
        if decoded:
            item.text = decoded
            count += 1

    return count


# ─────────────────────────────────────────────────────────────────────────────
# Priority 2
# ─────────────────────────────────────────────────────────────────────────────

_TEXT_SCAN_LABELS: set[DocItemLabel] = {DocItemLabel.TEXT, DocItemLabel.LIST_ITEM, DocItemLabel.SECTION_HEADER, DocItemLabel.CAPTION}
# PARAGRAPH 라벨이 있는 버전에서도 동작
try:
    _TEXT_SCAN_LABELS.add(DocItemLabel.PARAGRAPH)  # type: ignore[attr-defined]
except AttributeError:
    pass

def apply_crypto_patterns(doc: DoclingDocument) -> int:
    """TEXT 계열 요소에서 암호 도메인 수식 패턴을 탐지해 $...$ 로 래핑한다.

    FORMULA 아이템은 이미 Priority 1 에서 처리했으므로 건너뛴다.
    $...$ 로 이미 감싸인 부분은 재래핑하지 않는다.

    Returns:
        수정된 TEXT 아이템 수
    """
    count = 0
    for item in doc.texts:
        if item.label == DocItemLabel.FORMULA:
            continue
        if item.label not in _TEXT_SCAN_LABELS:
            continue
        if not item.text:
            continue

        new_text = _wrap_crypto_spans(item.text)
        if new_text != item.text:
            item.text = new_text
            count += 1

    return count


# ─────────────────────────────────────────────────────────────────────────────
# Priority 3
# ─────────────────────────────────────────────────────────────────────────────

def apply_table_cell_patterns(doc: DoclingDocument) -> int:
    """표 셀에서 수식 패턴을 탐지해 셀 텍스트를 $...$ 로 래핑한다.

    헤더행(row_index == 0) 은 건너뛴다.
    이미 $...$ 로 감싸인 셀은 그대로 둔다.

    Returns:
        수정된 표 셀 수
    """
    count = 0
    for item in doc.tables:
        if not isinstance(item, TableItem):
            continue
        tdata = item.data
        if tdata is None:
            continue
        for cell in tdata.table_cells:
            if cell.column_header or cell.row_header:
                continue  # 헤더 셀 건너뜀
            text = (cell.text or "").strip()
            if not text:
                continue
            if text.startswith("$"):
                continue  # 이미 래핑됨
            if _TABLE_CELL_FORMULA_PATTERN.search(text):
                cell.text = f"${text}$"
                count += 1

    return count


# ─────────────────────────────────────────────────────────────────────────────
# Priority 1b helpers
# ─────────────────────────────────────────────────────────────────────────────

def _normalize_latex_ws(text: str) -> str:
    """LaTeX 수식 공백 정규화: 줄별 다중 공백 축소, 중괄호 안 공백 제거."""
    lines = text.split("\n")
    result = []
    for line in lines:
        line = re.sub(r"[ \t]+", " ", line)     # 연속 공백 → 단일 공백
        line = re.sub(r"\{\s+", "{", line)       # { 바로 안쪽 공백 제거
        line = re.sub(r"\s+\}", "}", line)       # } 바로 안쪽 공백 제거
        result.append(line)
    return "\n".join(result).strip()


def normalize_formula_whitespace_in_doc(doc: DoclingDocument) -> int:
    """FORMULA 아이템의 LaTeX 공백을 정규화한다.

    Returns:
        수정된 FORMULA 아이템 수
    """
    count = 0
    for item in doc.texts:
        is_formula = (
            (FormulaItem is not None and isinstance(item, FormulaItem))
            or item.label == DocItemLabel.FORMULA
        )
        if not is_formula:
            continue
        if not item.text:
            continue
        new_text = _normalize_latex_ws(item.text)
        if new_text != item.text:
            item.text = new_text
            count += 1
    return count


# ─────────────────────────────────────────────────────────────────────────────
# Priority 4 helpers
# ─────────────────────────────────────────────────────────────────────────────

_FORMULA_FRAGMENT_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"^[A-Za-z]\d*$"),               # 단일 변수: X, V, Z0
    re.compile(r"^\d+$"),                        # 순수 숫자: 127
    re.compile(r"^[=|∥].+$"),                   # '='/'∥' 로 시작
    re.compile(r"^[|∥]{1,2}\s*[\dA-Za-z]+$"),  # '|| 0', '∥ x'
    re.compile(r"^[A-Za-z]\s+\d+$"),            # 'x 1', 'V 0'
    re.compile(r"^\d+\s*\.\s*$"),               # '1.', '2.'
    re.compile(r"^(?:for|from|to|do|in|if|then|else|end|while|return|let)$", re.IGNORECASE),
    re.compile(r"^(?:출력|입력)$"),              # 단독 한국어 단편
]

_HANGUL_RE = re.compile(r"[\uac00-\ud7a3\u1100-\u11ff\u3130-\u318f]")


def _is_formula_fragment(text: str) -> bool:
    """텍스트가 수식 조각인지 판별한다."""
    t = text.strip()
    if not t or len(t) > 20:
        return False
    # 한국어 포함 시 산문으로 간주 (단독 예외 제외)
    if t in {"출력", "입력"}:
        return True
    if _HANGUL_RE.search(t):
        return False
    return any(p.match(t) for p in _FORMULA_FRAGMENT_PATTERNS)


def suppress_formula_fragments(doc: DoclingDocument) -> int:
    """수식 조각으로 판별된 TEXT 아이템의 텍스트를 빈 문자열로 설정한다.

    짧고 한국어 없이 수식 변수처럼 보이는 텍스트 요소를 억제하여
    본문/헤딩 카운트 과다를 줄인다. FORMULA 아이템은 건너뛴다.

    Returns:
        억제된 아이템 수
    """
    count = 0
    for item in doc.texts:
        if item.label == DocItemLabel.FORMULA:
            continue
        if item.label not in _TEXT_SCAN_LABELS:
            continue
        if not item.text:
            continue
        if _is_formula_fragment(item.text):
            item.text = ""
            count += 1
    return count


# ─────────────────────────────────────────────────────────────────────────────
# Priority 5 helpers
# ─────────────────────────────────────────────────────────────────────────────

# 전각→반각 변환 테이블
_FULLWIDTH_MAP = str.maketrans(
    "（）｛｝［］　，。・",
    "(){}[] ,.·",  # 10 chars each; ・→· (middle dot U+00B7)
)


def _normalize_text(text: str) -> str:
    """NFC 정규화 + 하이픈 줄바꿈 복원 + 전각→반각."""
    text = unicodedata.normalize("NFC", text)
    text = re.sub(r"-\s*\n\s*", "", text)   # 하이픈 줄바꿈 복원
    text = text.translate(_FULLWIDTH_MAP)
    return text


def normalize_text_items(doc: DoclingDocument) -> int:
    """텍스트 아이템에 NFC, 줄바꿈 복원, 전각→반각 정규화를 적용한다.

    FORMULA 아이템은 건너뛴다.

    Returns:
        수정된 아이템 수
    """
    count = 0
    for item in doc.texts:
        if item.label == DocItemLabel.FORMULA:
            continue
        if not item.text:
            continue
        new_text = _normalize_text(item.text)
        if new_text != item.text:
            item.text = new_text
            count += 1
    return count


# ─────────────────────────────────────────────────────────────────────────────
# 통합 진입점
# ─────────────────────────────────────────────────────────────────────────────

def apply_all(doc: DoclingDocument, *, verbose: bool = False) -> dict[str, int]:
    """우선순위별 후처리를 순서대로 적용한다.

    Args:
        doc:     변환 완료된 DoclingDocument
        verbose: True 이면 각 단계 결과를 print 출력

    Returns:
        단계별 수정 항목 수 딕셔너리
    """
    r1 = fix_formula_items(doc)
    r1b = normalize_formula_whitespace_in_doc(doc)
    r2 = apply_crypto_patterns(doc)
    r3 = apply_table_cell_patterns(doc)
    # r4 = suppress_formula_fragments(doc)  # 비활성화: 유효 content 오억제 위험
    r4 = 0
    r5 = normalize_text_items(doc)

    if verbose:
        print(f"[post_process] Priority1  (formula)    : {r1} 항목 복원")
        print(f"[post_process] Priority1b (formula_ws) : {r1b} 항목 공백 정규화")
        print(f"[post_process] Priority2  (crypto)     : {r2} 항목 수정")
        print(f"[post_process] Priority3  (table)      : {r3} 셀 수정")
        print(f"[post_process] Priority4  (fragment)   : {r4} 항목 억제")
        print(f"[post_process] Priority5  (text_norm)  : {r5} 항목 정규화")

    return {
        "formula": r1,
        "formula_ws": r1b,
        "crypto": r2,
        "table_cell": r3,
        "fragment": r4,
        "text_norm": r5,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Priority 6: Markdown 텍스트 레벨 후처리
# ─────────────────────────────────────────────────────────────────────────────

# 헤딩 끝 숫자(페이지·절 번호 잔재) 제거: "## 암호알고리즘 목록 5" → "## 암호알고리즘 목록"
# 한국어 문자로 끝나는 헤딩 뒤에 붙은 1~2자리 숫자만 제거 (영문·특수문자 끝 헤딩은 건드리지 않음)
_HEADING_TRAILING_NUM = re.compile(r"^(#{1,6}\s+.{3,}[가-힣])\s+\d{1,2}$", re.MULTILINE)

# 오염 텍스트: "q [가-힣] [동일 가-힣]..." 이중 파싱 아티팩트 제거
# 예: "q 다 다음은" → "다음은",  "q 본 본 문서에서는" → "본 문서에서는"
_Q_DOUBLE_KO = re.compile(r"q ([가-힣]) \1")

# 원형 번호 이중 콤마 제거: "①, , ②, , ③" → "①, ②, ③"
_CIRCLED_DOUBLE_COMMA = re.compile(r"([①-⑳①②③④⑤⑥⑦⑧⑨⑩]),\s+,\s+")

# 일반 이중 콤마 제거 (표 셀 내 ", ," 패턴): "$A$, , 평문" → "$A$, 평문"
_DOUBLE_COMMA = re.compile(r",[ \t]+,[ \t]*")

# 수식 달러 앞 한국어: "$비트열", "$유한체" → 한국어만 남김
_DOLLAR_BEFORE_KO = re.compile(r"\$([가-힣])")

# 줄 끝 ". ." 정리
_DOT_DOT_EOL = re.compile(r"\.\s+\.\s*$")


def apply_md_cleanup(md: str, *, verbose: bool = False) -> tuple[str, dict[str, int]]:
    """마크다운 텍스트 수준 후처리.

    DoclingDocument 레벨 후처리(apply_all) 이후 저장된 마크다운 문자열에 적용한다.

    수정 항목:
      - SHA-2 표 분류 오류 수정 (블록암호 → 해시함수)
      - 'q [가-힣] [동일가-힣]' 이중 파싱 텍스트 제거
      - 헤딩 끝 불필요 숫자 제거
      - 원형 번호 / 일반 이중 콤마 정리
      - '$한국어' 달러 기호 제거
      - 제목 오염 문자열 제거
      - 줄 끝 '. .' 정리

    Returns:
        (cleaned_md, stats_dict) 튜플
    """
    stats: dict[str, int] = {}

    # 1. SHA-2 분류 오류: 표에서 블록암호로 잘못 분류된 SHA-2 행 수정
    sha2_count = md.count("| 블록암호 | SHA-2 |")
    md = md.replace("| 블록암호 | SHA-2 |", "| 해시함수 | SHA-2 |")
    stats["sha2_fix"] = sha2_count

    # 2. 'q [가-힣] [동일가-힣]' 이중 파싱 아티팩트 제거
    new_md = _Q_DOUBLE_KO.sub(r"\1", md)
    stats["q_double"] = len(_Q_DOUBLE_KO.findall(md))
    md = new_md

    # 3. 헤딩 끝 숫자(페이지·절 번호) 제거
    stats["trailing_num"] = len(_HEADING_TRAILING_NUM.findall(md))
    md = _HEADING_TRAILING_NUM.sub(r"\1", md)

    # 4. 원형 번호 이중 콤마: "①, , ②" → "①, ②"
    stats["circled_comma"] = len(_CIRCLED_DOUBLE_COMMA.findall(md))
    md = _CIRCLED_DOUBLE_COMMA.sub(r"\1, ", md)

    # 5. 일반 이중 콤마: "$A$, , 평문" → "$A$, 평문"
    stats["double_comma"] = len(_DOUBLE_COMMA.findall(md))
    md = _DOUBLE_COMMA.sub(", ", md)

    # 6. '$한국어' 달러 기호 제거: "$비트열" → "비트열"
    stats["dollar_ko"] = len(_DOLLAR_BEFORE_KO.findall(md))
    md = _DOLLAR_BEFORE_KO.sub(r"\1", md)

    # 7. 제목 오염 문자열 제거: "GVeuniddeo rf oImr" (커버 페이지 영문 잡음)
    if "GVeuniddeo rf oImr" in md:
        md = md.replace("GVeuniddeo rf oImr", "")
        # 해당 줄의 행 끝 공백 정리
        md = re.sub(r"(암호모듈 구현안내서)\s+$", r"\1", md, flags=re.MULTILINE)
        stats["title_corrupt"] = 1
    else:
        stats["title_corrupt"] = 0

    # 8. 줄 끝 '. .' 정리: "다음과 같다. ." → "다음과 같다."
    lines = md.split("\n")
    new_lines = []
    dot_count = 0
    for line in lines:
        new_line = _DOT_DOT_EOL.sub(".", line)
        if new_line != line:
            dot_count += 1
        new_lines.append(new_line)
    stats["dot_dot"] = dot_count
    md = "\n".join(new_lines)

    if verbose:
        for k, v in stats.items():
            if v:
                print(f"[md_cleanup] {k:<18}: {v}")

    return md, stats


# ─────────────────────────────────────────────────────────────────────────────
# Priority 7: 그림 캡션 추출 + 주입
# kcmvp_rag_v4.1 FigureExtractor._find_caption 캐스케이드 차용
# ─────────────────────────────────────────────────────────────────────────────

# "그림 N", "Figure N", "Fig. N", "도형 N", "도면 N" 캡션 패턴
_CAPTION_PAT = re.compile(
    r"(그림|Figure|Fig\.?|도[형면]|[Pp]icture)\s*[-.]?\s*\d*",
    re.IGNORECASE,
)

# 마크다운 이미지 라인 패턴: ![alt](path)
_IMAGE_LINE_RE = re.compile(r"^!\[.*?\]\((.+)\)\s*$")


def extract_figure_captions(doc) -> list[str | None]:
    """DoclingDocument에서 picture별 캡션 텍스트를 추출한다.

    3단계 캐스케이드 (kcmvp_rag_v4.1 FigureExtractor._find_caption 참고):
      1단계: picture.captions → docling이 직접 연결한 CAPTION 아이템
      2단계: 인접 아이템(±5) 중 CAPTION 레이블 또는 '그림 N' 패턴 텍스트
      3단계: 직전 HEADING/SECTION_HEADER 아이템 텍스트 (fallback)

    Returns:
        len == len(doc.pictures) 인 리스트, None이면 캡션 없음
    """
    items_flat = list(doc.iterate_items())  # [(item, level), ...]

    # picture/chart 아이템이 등장하는 인덱스 목록 (문서 순서 = doc.pictures 순서)
    pic_item_indices: list[int] = []
    for i, (item, _) in enumerate(items_flat):
        lbl = str(getattr(item, "label", "")).lower()
        if "picture" in lbl or "chart" in lbl:
            pic_item_indices.append(i)

    captions: list[str | None] = [None] * len(doc.pictures)

    for pic_idx, item_i in enumerate(pic_item_indices):
        if pic_idx >= len(captions):
            break
        pic_item, _ = items_flat[item_i]

        # 1단계: docling 직접 연결 caption
        for cap_ref in getattr(pic_item, "captions", []):
            cap_item = doc.get_ref(cap_ref)
            if cap_item and getattr(cap_item, "text", None):
                captions[pic_idx] = cap_item.text.strip()
                break
        if captions[pic_idx]:
            continue

        # 2단계: 인접 아이템 탐색 (앞1 뒤1 앞2 뒤2 ... 앞5 뒤5)
        for offset in (1, -1, 2, -2, 3, -3, 4, -4, 5, -5):
            ni = item_i + offset
            if not (0 <= ni < len(items_flat)):
                continue
            nb, _ = items_flat[ni]
            nb_lbl = str(getattr(nb, "label", "")).lower()
            nb_text = getattr(nb, "text", None)
            if nb_text and ("caption" in nb_lbl or _CAPTION_PAT.search(nb_text)):
                captions[pic_idx] = nb_text.strip()
                break
        if captions[pic_idx]:
            continue

        # 3단계: 직전 heading fallback (최대 15 아이템 역방향 탐색)
        for prev_i in range(item_i - 1, max(item_i - 15, -1), -1):
            prev, _ = items_flat[prev_i]
            prev_lbl = str(getattr(prev, "label", "")).lower()
            prev_text = getattr(prev, "text", None)
            if ("heading" in prev_lbl or "section" in prev_lbl) and prev_text:
                captions[pic_idx] = prev_text.strip()
                break

    return captions


def inject_figure_captions(
    md: str,
    captions: list[str | None],
    *,
    fig_offset: int = 0,
) -> tuple[str, int]:
    """마크다운의 각 ![...](path) 앞에 **그림 N: caption** 줄을 삽입한다.

    kcmvp_rag_v4.1 MarkdownRenderer._render_figure_inline 방식 차용.
    - 이미 '**그림'으로 시작하는 줄이 바로 위에 있으면 중복 삽입을 건너뛴다.
    - alt 텍스트를 캡션으로 교체하여 마크다운 뷰어에서 툴팁으로도 표시된다.

    Args:
        md: 마크다운 문자열 (apply_all 이후 export된 것)
        captions: extract_figure_captions() 반환값 (picture 순서)
        fig_offset: 청크 변환 시 그림 번호 시작 오프셋 (0-based index)

    Returns:
        (annotated_md, injected_count)
    """
    lines = md.split("\n")
    result: list[str] = []
    fig_count = fig_offset
    injected = 0

    for line in lines:
        m = _IMAGE_LINE_RE.match(line)
        if m:
            # 직전 줄이 이미 '**그림'으로 시작하면 중복 삽입 방지
            if result and result[-1].lstrip().startswith("**그림"):
                result.append(line)
                fig_count += 1
                continue

            # 캡션 결정
            cap_text = (
                captions[fig_count]
                if fig_count < len(captions) and captions[fig_count] is not None
                else None
            )
            caption = cap_text if cap_text else "(캡션 없음)"
            label = fig_count + 1

            result.append(f"**그림 {label}: {caption}**")
            result.append("")
            # alt 텍스트는 원본 유지 (평가 지표와 호환성 유지)
            result.append(line)
            fig_count += 1
            injected += 1
        else:
            result.append(line)

    return "\n".join(result), injected
