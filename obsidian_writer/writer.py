"""Obsidian Vault 에 노트를 쓴다.

## 지키는 것

1. **Vault 는 사용자의 실제 데이터다.** 기본 충돌 정책은 `skip` 이고, 덮어쓰기는
   `config.yaml: output.on_conflict` 에서 명시적으로 켜야만 동작한다. (D-028)
2. **원자적으로 쓴다.** 같은 디렉터리의 임시 파일에 쓰고 `os.replace` 로 옮긴다.
   중간에 죽어도 반쪽 노트가 남지 않는다.
3. **Windows 경로를 안전하게 다룬다.** 금지 문자(`\\ / : * ? " < > |`)와 끝의
   점·공백을 파일명에서 제거한다.
"""

from __future__ import annotations

import os
import re
import sys
import tempfile
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from extraction.schema import NewsOntology
from obsidian_writer.mapper import NoteContext, render_note

# 제목 슬러그 최대 길이(하이픈 포함). 하이픈 경계에서 자른다.
#
# 24자였을 때 한국어 제목이 뜻이 끝나기 전에 잘렸다 — 실제로 만들어진 파일명이
# `...-퍼저가-ffmpeg의.md` 로, 조사에서 끊겨 무슨 기사인지 읽히지 않았다.
# 원인은 자르는 로직이 아니라 **길이 기준**이다. "영문 20자 내외"로 잡은 값인데,
# 한국어는 한 글자가 한 음절이라 같은 글자 수에 단어가 훨씬 적게 들어간다.
#
# 실측: 수집 중인 한국어 헤드라인은 슬러그로 바꾸면 34~35자에 몰려 있다.
# 40 이면 그 대역이 통째로 들어가면서 파일명 전체(`날짜-소스-슬러그.md`)도
# 60자 안쪽에 머문다. 더 늘려도 얻는 건 영문 논문 제목뿐인데, 그건 애초에
# 40~50자로 끝나지 않아서 어차피 잘린다. (D-037)
SLUG_MAX_CHARS = 40

# Windows 파일명 금지 문자 + 제어문자.
_FORBIDDEN_RE = re.compile(r'[\\/:*?"<>|\x00-\x1f]')
# 슬러그에서 남길 문자: 한글/영숫자/하이픈. 그 외는 구분자로 취급한다.
_NON_SLUG_RE = re.compile(r"[^0-9a-z가-힣ㄱ-ㅎㅏ-ㅣ]+")
_DASH_RUN_RE = re.compile(r"-{2,}")

ConflictPolicy = Literal["skip", "overwrite", "version"]


class WriteError(Exception):
    """노트를 쓰지 못했다."""


@dataclass(frozen=True)
class WriteResult:
    """쓰기 1건의 결과."""

    path: Path
    written: bool
    reason: str = ""

    @property
    def skipped(self) -> bool:
        return not self.written


# ---------------------------------------------------------------------------
# 슬러그
# ---------------------------------------------------------------------------
def slugify(text: str, *, max_chars: int | None = None) -> str:
    """제목/소스명을 파일명 조각으로 바꾼다.

    한글은 **로마자로 옮기지 않고 그대로 둔다.** 로마자 변환은 표기법이 여러 개라
    같은 제목이 실행마다 달라질 수 있고, 변환된 파일명은 사람이 못 읽는다.
    (D-031)

    - 소문자화, 공백·특수문자는 하이픈으로
    - 한글 자모/완성형과 영숫자만 남긴다
    - `max_chars` 가 있으면 **하이픈 경계에서** 자른다 (단어를 반토막 내지 않는다)
    """
    # NFC 로 정규화해야 자모 분리된 한글(맥 파일명 등)이 완성형으로 모인다.
    normalized = unicodedata.normalize("NFC", text).lower()
    slug = _NON_SLUG_RE.sub("-", normalized)
    slug = _DASH_RUN_RE.sub("-", slug).strip("-")

    if max_chars is not None:
        slug = _truncate_at_word_boundary(slug, max_chars)

    return slug


def _truncate_at_word_boundary(slug: str, max_chars: int) -> str:
    """하이픈(=원래 공백) 경계에서 자른다.

    `max_chars + 1` 까지 들여다보는 이유: 경계가 정확히 `max_chars` 위치에 있으면
    그 앞 단어는 길이 제한 안에 **온전히** 들어간다. `slug[:max_chars]` 만 보면
    그 하이픈이 시야 밖이라 멀쩡한 단어를 하나 더 버리게 된다.

    경계가 너무 앞에 있으면(첫 단어가 길어서 남는 게 토막뿐이면) 경계를 포기하고
    글자 수로 자른다. 하이픈이 아예 없는 통짜 제목도 같은 길로 간다 — 자를 자리가
    없으니 달리 방법이 없다. 한글은 음절 단위라 중간에서 잘려도 글자가 깨지지는
    않는다.
    """
    if len(slug) <= max_chars:
        return slug

    window = slug[: max_chars + 1]
    boundary = window.rfind("-")
    if boundary >= max_chars // 2:
        return window[:boundary].strip("-")
    return slug[:max_chars].strip("-")


def sanitize_filename(name: str) -> str:
    """Windows 에서 안전한 파일명으로 만든다."""
    cleaned = _FORBIDDEN_RE.sub("", name)
    # 끝의 점·공백은 Windows 가 조용히 잘라내므로 미리 제거한다.
    cleaned = cleaned.rstrip(". ")
    return cleaned or "untitled"


def build_filename(
    *,
    title: str,
    source_name: str,
    processed_at: datetime,
    template: str = "{date}-{source}-{slug}.md",
) -> str:
    """`{날짜}-{소스}-{slug}.md` 형태의 파일명."""
    name = template.format(
        date=processed_at.date().isoformat(),
        source=slugify(source_name) or "unknown",
        slug=slugify(title, max_chars=SLUG_MAX_CHARS) or "untitled",
    )
    return sanitize_filename(name)


# ---------------------------------------------------------------------------
# 충돌 해소
# ---------------------------------------------------------------------------
def _existing_source_url(path: Path) -> str | None:
    """이미 있는 노트의 frontmatter 에서 `source_url` 만 싸게 읽는다.

    YAML 전체를 파싱하지 않는 이유: 사용자가 Obsidian 에서 손으로 고친 노트가
    파싱되지 않을 수 있는데, 그것 때문에 쓰기가 실패하면 안 된다.
    """
    try:
        with open(path, encoding="utf-8") as f:
            if f.readline().strip() != "---":
                return None
            for line in f:
                if line.strip() == "---":
                    return None
                if line.startswith("source_url:"):
                    return line.split(":", 1)[1].strip().strip("'\"")
    except OSError:
        return None
    return None


def resolve_path(directory: Path, filename: str, *, source_url: str) -> tuple[Path, bool]:
    """실제로 쓸 경로와 "같은 기사인가" 여부를 정한다.

    슬러그가 겹치는 경우는 두 가지이고, 처리가 다르다.

    1. **같은 기사** (`source_url` 일치) — 재실행이다. 새 파일을 만들지 않는다.
       충돌 정책(`skip`/`overwrite`)이 여기에 적용된다.
    2. **다른 기사인데 슬러그만 같음** — 제목이 비슷할 뿐 별개 노트다.
       `-2`, `-3` 을 붙여 새 경로를 찾는다.

    Returns:
        `(경로, 같은 기사인가)`
    """
    base = directory / filename
    if not base.exists():
        return base, False
    if _existing_source_url(base) == source_url:
        return base, True

    stem, suffix = base.stem, base.suffix
    for n in range(2, 1000):
        candidate = directory / f"{stem}-{n}{suffix}"
        if not candidate.exists():
            return candidate, False
        if _existing_source_url(candidate) == source_url:
            return candidate, True
    raise WriteError(f"슬러그 충돌을 999회 안에 해소하지 못했습니다: {filename}")


# ---------------------------------------------------------------------------
# 쓰기
# ---------------------------------------------------------------------------
def atomic_write(path: Path, content: str) -> None:
    """같은 디렉터리의 임시 파일에 쓰고 rename 한다.

    같은 디렉터리를 쓰는 이유: `os.replace` 는 같은 볼륨 안에서만 원자적이다.
    시스템 임시 폴더에 썼다가 옮기면 볼륨이 달라져 보장이 깨진다.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=".tmp-", suffix=".md")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def resolve_output_dir(
    vault_path: str | Path | None = None,
    *,
    config: dict[str, Any] | None = None,
) -> Path:
    """노트를 쓸 디렉터리를 정한다.

    `.env: OBSIDIAN_VAULT_PATH` 가 기준이고, `config.yaml: output.vault_subdir`
    가 비어 있지 않으면 그 아래로 내려간다. 환경변수가 노트 디렉터리를 직접
    가리키는 설정이면 `vault_subdir` 를 비워 둔다.
    """
    if vault_path is None:
        from extraction.llm import load_env  # 순환 import 회피 위해 지역 import

        load_env()
        vault_path = os.environ.get("OBSIDIAN_VAULT_PATH")
    if not vault_path:
        raise WriteError(
            "OBSIDIAN_VAULT_PATH 가 없습니다. .env.example 을 .env 로 복사해 채우세요."
        )

    directory = Path(vault_path).expanduser()
    subdir = ((config or {}).get("output") or {}).get("vault_subdir") or ""
    if subdir:
        directory = directory / subdir
    return directory


def write_note(
    ontology: NewsOntology,
    context: NoteContext,
    *,
    output_dir: Path,
    on_conflict: ConflictPolicy = "skip",
    filename_template: str = "{date}-{source}-{slug}.md",
) -> WriteResult:
    """노트 1건을 쓴다.

    Args:
        output_dir: 노트를 쓸 디렉터리. 없으면 만든다.
        on_conflict: 같은 기사 노트가 이미 있을 때 — `skip`(기본) / `overwrite`.
                     `version` 은 아직 구현하지 않았다.
    """
    processed_at = context.resolved_processed_at()
    filename = build_filename(
        title=context.item.title,
        source_name=context.item.source_name,
        processed_at=processed_at,
        template=filename_template,
    )
    source_url = str(context.item.url)

    output_dir.mkdir(parents=True, exist_ok=True)
    path, is_same_article = resolve_path(output_dir, filename, source_url=source_url)

    if is_same_article:
        if on_conflict == "skip":
            print(f"[skip] 이미 있는 노트: {path.name}", file=sys.stderr)
            return WriteResult(path=path, written=False, reason="already_exists")
        if on_conflict == "version":
            raise WriteError("on_conflict='version' 은 아직 구현되지 않았습니다")
        if on_conflict != "overwrite":
            raise WriteError(f"알 수 없는 on_conflict 값: {on_conflict!r}")

    atomic_write(path, render_note(ontology, context))
    return WriteResult(path=path, written=True, reason="overwritten" if is_same_article else "")
