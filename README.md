# kcmvp_combine

**docling** 기반 한국어 PDF(한글 수식/표 포함) → Markdown 변환 도구.

[docling](https://github.com/docling-project/docling) 2.80.0에 **Fix17 패치**
(PUA 수식 디코딩 · 한국어 공백 복원 · 표/읽기순서 개선)를 적용하고,
암호/수식 패턴 후처리를 추가한 standalone 레포입니다.

---

## 설치

### 1. Python 가상환경 생성 (Python 3.10–3.12 권장)

```bash
python -m venv .venv
source .venv/bin/activate        # Linux / macOS
# .venv\Scripts\activate         # Windows PowerShell
```

### 2. 의존성 설치

```bash
pip install -r requirements.txt
```

> `PyMuPDF` 가 없으면 단일 패스로만 변환됩니다 (45 페이지 이하 PDF에서는 동일함).

### 3. Fix17 패치 적용 (최초 1회)

`run_convert.py` 첫 실행 시 **자동으로** 패치를 적용합니다.  
수동으로 적용하려면:

```bash
python apply_patches.py
```

> `pip install -r requirements.txt` 이후 docling 을 재설치하면 패치가 초기화됩니다.  
> 그때는 다시 `python apply_patches.py` 를 실행하세요.

---

## 사용법

```bash
python run_convert.py <input_pdf> <output_dir>
```

**예시**

```bash
python run_convert.py /data/notice.pdf ./output/
```

결과 파일: `<output_dir>/<stem>.md`

---

## 패치 내용 (Fix17)

`patches/` 디렉터리에 수정된 3개 파일이 포함됩니다.

| 파일 | 변경 내용 |
|---|---|
| `docling_parse_backend.py` | HyhwpEQ PUA 문자(0xE033–0xE0B1) 디코딩, 한국어 공백 복원 |
| `readingorder_model.py` | 읽기 순서 모델 헤딩 레벨 개선 |
| `table_structure_model.py` | 표 구조 인식 개선 |

이 파일들은 설치된 docling 패키지의 해당 파일을 대체합니다.

---

## 후처리 단계 (post_process.py)

| 우선순위 | 함수 | 설명 |
|---|---|---|
| 1 | `fix_formula_items` | FORMULA 블록 텍스트 복원 |
| 1b | `normalize_formula_whitespace_in_doc` | LaTeX 공백 정규화 |
| 2 | `apply_crypto_patterns` | 암호 도메인 수식 → `$...$` 래핑 |
| 3 | `apply_table_cell_patterns` | 표 셀 수식 → `$...$` 래핑 |
| 5 | `normalize_text_items` | NFC · 하이픈 줄바꿈 복원 · 전각→반각 |

---

## 환경

- Python 3.10–3.12
- docling 2.80.0
- CUDA GPU (선택, CPU-only 동작 가능하지만 느림)
- WSL2 Ubuntu 또는 Linux 권장

---

## 파일 구조

```
kcmvp_combine/
├── apply_patches.py        # Fix17 패치 적용 스크립트
├── post_process.py         # 후처리 단계 구현
├── requirements.txt        # 의존성
├── run_convert.py          # 변환 진입점
└── patches/                # Fix17 패치 파일 (docling 2.80.0 기준)
    ├── docling_parse_backend.py
    ├── readingorder_model.py
    └── table_structure_model.py
```
