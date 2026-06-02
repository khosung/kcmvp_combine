"""Fix17 patches를 설치된 docling 패키지에 적용한다.

설치 직후 한 번만 실행하면 됩니다:
    python apply_patches.py

run_convert.py 를 실행할 때 자동으로 호출되므로 직접 실행하지 않아도 됩니다.
"""
from __future__ import annotations

import importlib.util
import shutil
import sys
from pathlib import Path

# patches/ 디렉토리 → 설치된 docling 모듈 경로 매핑
_PATCH_MAP: dict[str, str] = {
    "docling.backend.docling_parse_backend": "docling_parse_backend.py",
    "docling.models.stages.reading_order.readingorder_model": "readingorder_model.py",
    "docling.models.stages.table_structure.table_structure_model": "table_structure_model.py",
}

_PATCHES_DIR = Path(__file__).parent / "patches"
_SENTINEL = "# Fix17-patched"  # 패치 여부 판별용 마커


def is_patched(target: Path) -> bool:
    """대상 파일이 이미 Fix17 패치가 적용됐는지 확인한다."""
    try:
        first_lines = target.read_text(encoding="utf-8", errors="ignore")[:200]
        return _SENTINEL in first_lines
    except OSError:
        return False


def apply(force: bool = False, quiet: bool = False) -> list[str]:
    """Fix17 패치를 적용하고 변경된 파일 목록을 반환한다.

    Args:
        force: True이면 이미 패치된 파일도 덮어쓴다.
        quiet: True이면 출력을 억제한다.
    Returns:
        패치가 적용된 파일 경로 목록.
    """
    applied: list[str] = []

    for module_dotted, patch_filename in _PATCH_MAP.items():
        patch_src = _PATCHES_DIR / patch_filename
        if not patch_src.exists():
            if not quiet:
                print(f"[apply_patches] WARNING: patch file not found: {patch_src}")
            continue

        try:
            spec = importlib.util.find_spec(module_dotted)
        except (ModuleNotFoundError, ValueError):
            spec = None
        if spec is None or not spec.origin:
            if not quiet:
                print(f"[apply_patches] WARNING: module not found: {module_dotted}")
            continue

        target = Path(spec.origin)
        if not force and is_patched(target):
            if not quiet:
                print(f"[apply_patches] Already patched, skipping: {target.name}")
            continue

        shutil.copy(str(patch_src), str(target))
        applied.append(str(target))
        if not quiet:
            print(f"[apply_patches] Applied: {patch_filename} → {target}")

    return applied


def _add_sentinel_if_needed() -> None:
    """patches/ 디렉토리 내 파일에 _SENTINEL이 없으면 첫 줄에 추가한다."""
    for patch_file in _PATCHES_DIR.glob("*.py"):
        text = patch_file.read_text(encoding="utf-8")
        if _SENTINEL not in text:
            patch_file.write_text(_SENTINEL + "\n" + text, encoding="utf-8")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Fix17 docling 패치 적용")
    parser.add_argument("--force", action="store_true", help="이미 패치된 파일도 덮어쓰기")
    args = parser.parse_args()

    _add_sentinel_if_needed()
    changed = apply(force=args.force)
    if changed:
        print(f"\nFix17 패치 완료: {len(changed)}개 파일 적용됨")
    else:
        print("\n패치할 파일 없음 (이미 최신 상태)")
