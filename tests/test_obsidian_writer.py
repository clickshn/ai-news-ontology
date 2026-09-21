"""obsidian_writer 단위 테스트.

**실제 Vault 에는 절대 쓰지 않는다.** 모든 쓰기는 pytest 의 `tmp_path` 안에서만
일어난다. `write_note` 는 `output_dir` 를 필수 인자로 받으므로 환경변수를 실수로
읽어 실제 Vault 를 건드릴 경로가 없다.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path

import pytest
import yaml

from collectors.base import RawItem
from extraction.schema import NewsOntology
from obsidian_writer.mapper import (
    FRONTMATTER_ORDER,
    NoteContext,
    build_frontmatter,
    dump_frontmatter,
    render_body,
    render_note,
)
from obsidian_writer.writer import (
    SLUG_MAX_CHARS,
    WriteError,
    atomic_write,
    build_filename,
    resolve_path,
    sanitize_filename,
    slugify,
    write_note,
)

PROCESSED_AT = datetime(2026, 8, 29, 20, 45, tzinfo=timezone.utc)

ONTOLOGY_PAYLOAD = {
    "요약": "FFmpeg의 Sony PS2 VPK 디먹서에서 0 나누기 버그가 발견됐다. 악성 파일을 여는 애플리케이션이 충돌할 수 있다.",
    "기술영역": ["Application/Product"],
    "발표유형": "Community/Discussion",
    "관련기업": [{"원문표기": "오픈AI", "역할": "발표 주체"}, {"원문표기": "FFmpeg"}],
    "관련기존기술": ["Fuzzing", "Vibe Coding"],
    "영향도": {"점수": 2, "근거": "파급은 국소적이나 LLM 보조 퍼징의 실제 적용 사례다."},
}


@pytest.fixture
def ontology() -> NewsOntology:
    return NewsOntology.model_validate(ONTOLOGY_PAYLOAD)


@pytest.fixture
def item() -> RawItem:
    return RawItem(
        url="https://news.hada.io/topic?id=33001",
        title="바이브코딩으로 만든 퍼저가 FFmpeg의 0 나누기 버그를 발견",
        body="본문",
        source_name="GeekNews",
        published_at=date(2026, 8, 29),
        collected_at=date(2026, 8, 29),
    )


@pytest.fixture
def context(item) -> NoteContext:
    return NoteContext(
        item=item,
        extraction_model="claude-opus-5",
        prompt_version="extract_ontology.v3.md",
        processed_at=PROCESSED_AT,
    )


# ---------------------------------------------------------------------------
# mapper — 키 변환
# ---------------------------------------------------------------------------
def test_frontmatter_uses_english_keys(ontology, context):
    fm = build_frontmatter(ontology, context)
    assert set(fm) <= set(FRONTMATTER_ORDER)
    for key in fm:
        assert not any("\uac00" <= ch <= "\ud7a3" for ch in key), f"한글 키: {key}"


def test_frontmatter_key_order_is_stable(ontology, context):
    fm = build_frontmatter(ontology, context)
    assert list(fm) == [k for k in FRONTMATTER_ORDER if k in fm]


def test_impact_is_flattened_into_two_keys(ontology, context):
    """중첩 객체가 아니라 평평한 두 키여야 Dataview 가 정렬·필터할 수 있다 (D-007)."""
    fm = build_frontmatter(ontology, context)
    assert fm["impact_score"] == 2
    assert "LLM 보조 퍼징" in fm["impact_rationale"]
    assert "impact" not in fm


def test_list_fields_are_lists(ontology, context):
    fm = build_frontmatter(ontology, context)
    assert fm["tech_domain"] == ["Application/Product"]
    assert fm["prior_art"] == ["Fuzzing", "Vibe Coding"]
    assert isinstance(fm["companies"], list)


def test_release_type_is_plain_string(ontology, context):
    """Enum 이 그대로 직렬화되면 YAML 에 파이썬 객체 태그가 박힌다."""
    fm = build_frontmatter(ontology, context)
    assert fm["release_type"] == "Community/Discussion"
    assert isinstance(fm["release_type"], str)


def test_companies_use_canonical_not_raw(ontology, context):
    """정규화(D-025)를 해 놓고 원문표기로 실으면 그래프에서 도로 갈라진다 (D-030)."""
    fm = build_frontmatter(ontology, context)
    assert fm["companies"] == ["OpenAI", "FFmpeg"]
    assert "오픈AI" not in fm["companies"]


def test_summary_is_not_in_frontmatter(ontology, context):
    """요약은 본문 '## 요약' 섹션으로 간다."""
    assert "summary" not in build_frontmatter(ontology, context)


# ---------------------------------------------------------------------------
# mapper — YAML 직렬화
# ---------------------------------------------------------------------------
def test_korean_survives_yaml_dump(ontology, context):
    """allow_unicode=True 가 없으면 한글이 \\uXXXX 로 깨진다."""
    text = dump_frontmatter(build_frontmatter(ontology, context))
    assert "\\u" not in text
    assert "바이브코딩" in text
    assert "국소적" in text


def test_yaml_round_trips(ontology, context):
    fm = build_frontmatter(ontology, context)
    reloaded = yaml.safe_load(dump_frontmatter(fm))
    assert reloaded == fm


def test_long_rationale_is_not_line_wrapped(item):
    """줄바꿈으로 접히면 Obsidian Properties 가 값을 잘못 읽는 경우가 있다."""
    payload = {**ONTOLOGY_PAYLOAD, "영향도": {"점수": 3, "근거": "가" * 250}}
    text = dump_frontmatter(
        build_frontmatter(NewsOntology.model_validate(payload), NoteContext(item=item))
    )
    rationale_lines = [ln for ln in text.splitlines() if ln.startswith("impact_rationale:")]
    assert len(rationale_lines) == 1
    assert "가" * 250 in rationale_lines[0]


# ---------------------------------------------------------------------------
# mapper — 본문
# ---------------------------------------------------------------------------
def test_body_has_three_sections(ontology, context):
    body = render_body(ontology, context)
    assert "## 요약" in body and "## 관련 개념" in body and "## 원문" in body


def test_wikilinks_cover_concepts_and_companies(ontology, context):
    body = render_body(ontology, context)
    assert "[[Fuzzing]]" in body
    assert "[[Vibe Coding]]" in body
    assert "[[OpenAI]]" in body       # canonical
    assert "[[오픈AI]]" not in body    # 원문표기로 링크하지 않는다


def test_empty_concepts_render_placeholder(item):
    payload = {**ONTOLOGY_PAYLOAD, "관련기존기술": [], "관련기업": []}
    body = render_body(NewsOntology.model_validate(payload), NoteContext(item=item))
    assert "- (없음)" in body


def test_wikilink_strips_breaking_characters(item):
    payload = {**ONTOLOGY_PAYLOAD, "관련기존기술": ["A]]B|C"]}
    body = render_body(NewsOntology.model_validate(payload), NoteContext(item=item))
    assert "[[AB C]]" in body


def test_wikilink_strips_heading_separator(item):
    """`#` 도 `|` 와 같이 지운다 (F8).

    `[[RAG#평가]]` 는 **깨진 링크가 아니다** — `RAG` 노트의 `평가` 절을 가리키는
    멀쩡한 링크다. 그래서 오류가 나지 않고, 대신 개념 하나가 엉뚱한 노트의
    소제목으로 빨려 들어간다. `|` 와 같은 이유로 같은 자리에서 막는다.
    """
    payload = {**ONTOLOGY_PAYLOAD, "관련기존기술": ["RAG#평가"]}
    body = render_body(NewsOntology.model_validate(payload), NoteContext(item=item))
    assert "[[RAG 평가]]" in body
    assert "[[RAG#" not in body


def test_wikilink_strips_heading_separator_in_company_links(item):
    """기업 링크도 같은 함수를 지난다 — 한쪽만 고치면 그래프가 갈린다 (D-030)."""
    payload = {**ONTOLOGY_PAYLOAD, "관련기업": [{"원문표기": "A#B", "역할": "개발"}]}
    body = render_body(NewsOntology.model_validate(payload), NoteContext(item=item))
    assert "[[A B]]" in body


def test_render_note_starts_with_frontmatter(ontology, context):
    note = render_note(ontology, context)
    assert note.startswith("---\n")
    assert note.count("\n---\n") == 1


# ---------------------------------------------------------------------------
# writer — 슬러그
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("GeekNews", "geeknews"),
        ("OpenAI News", "openai-news"),
        ("arXiv cs.CL (Atom API)", "arxiv-cs-cl-atom-api"),
        ("Hugging Face Blog", "hugging-face-blog"),
    ],
)
def test_source_slugs(text, expected):
    assert slugify(text) == expected


def test_korean_title_stays_korean():
    """로마자로 옮기지 않는다 — 표기법이 여러 개라 실행마다 달라진다 (D-031)."""
    slug = slugify("바이브코딩으로 만든 퍼저가 버그를 발견", max_chars=24)
    assert "바이브코딩으로" in slug
    assert len(slug) <= 24


@pytest.mark.parametrize(
    "title",
    [
        'C:\\경로\\제목?*"<>|',
        "슬래시/포함/제목",
        "점으로 끝나는 제목...",
        "   공백   투성이   ",
        "느낌표!!! 물음표??? 콜론:::",
    ],
)
def test_special_characters_removed_from_filename(title):
    name = build_filename(title=title, source_name="GeekNews", processed_at=PROCESSED_AT)
    assert not set(name) & set('\\/:*?"<>|')
    assert name.endswith(".md")
    assert not name.replace(".md", "").endswith((".", " "))


def test_slug_truncates_at_hyphen_boundary():
    slug = slugify("alpha beta gamma delta epsilon zeta", max_chars=24)
    assert len(slug) <= 24
    assert not slug.endswith("-")
    # 단어가 반토막 나지 않았는지
    assert all(part in "alpha beta gamma delta epsilon zeta".split() for part in slug.split("-"))


# ---------------------------------------------------------------------------
# writer — 슬러그 자르기 (D-037)
#
# 24자였을 때 실제로 나온 파일명이 `...-퍼저가-ffmpeg의.md` 였다. 조사에서 끊겨
# 무슨 기사인지 읽히지 않는다. 아래는 길이 기준(40)과 경계 규칙을 함께 고정한다.
# ---------------------------------------------------------------------------
REGRESSION_TITLE = "바이브코딩으로 만든 퍼저가 FFmpeg의 0 나누기 버그를 발견"


def test_default_slug_length_is_forty():
    """숫자를 바꾸려면 근거가 바뀌어야 한다 — D-037 을 함께 고쳐라."""
    assert SLUG_MAX_CHARS == 40


def test_regression_title_is_no_longer_cut_mid_thought():
    """이 버그의 원본 사례. 이제 제목이 통째로 들어간다."""
    slug = slugify(REGRESSION_TITLE, max_chars=SLUG_MAX_CHARS)
    assert slug == "바이브코딩으로-만든-퍼저가-ffmpeg의-0-나누기-버그를-발견"
    assert not slug.endswith("의"), "조사에서 끊겼다 — 24자 시절의 증상이다"


def test_regression_filename_stays_short_enough():
    """Windows 경로 예산과 Obsidian 파일 목록 가독성을 위해 60자 안쪽."""
    name = build_filename(
        title=REGRESSION_TITLE, source_name="GeekNews", processed_at=PROCESSED_AT
    )
    assert name == "2026-08-29-geeknews-바이브코딩으로-만든-퍼저가-ffmpeg의-0-나누기-버그를-발견.md"
    assert len(name) < 64


@pytest.mark.parametrize(
    ("label", "title"),
    [
        ("짧은 한글", "Anthropic, Claude Opus 5 출시"),
        ("보통 한글", "SpaceX의 Cursor 인수 이후 OpenAI가 내린 결정"),
        ("긴 한글", "EasyEffects를 모든 Linux 배포판에 포함해 노트북 스피커 음질을 개선해야 함"),
        ("영문", "Test-Time Scaling without Repeated Sampling for Efficient Reasoning"),
        ("혼합", "Anthropic이 공개한 Claude Agent SDK로 사내 워크플로 자동화하기"),
    ],
)
def test_slug_never_ends_mid_word(label, title):
    """어떤 제목이든 (a) 길이를 지키고 (b) 하이픈으로 끝나지 않는다."""
    slug = slugify(title, max_chars=SLUG_MAX_CHARS)
    assert 0 < len(slug) <= SLUG_MAX_CHARS, label
    assert not slug.startswith("-") and not slug.endswith("-"), label


@pytest.mark.parametrize(
    ("label", "title"),
    [
        ("짧은 한글", "Anthropic, Claude Opus 5 출시"),
        ("보통 한글", "SpaceX의 Cursor 인수 이후 OpenAI가 내린 결정"),
        ("혼합", "Anthropic이 공개한 Claude Agent SDK"),
    ],
)
def test_short_titles_survive_intact(label, title):
    """40자 안에 드는 제목은 한 글자도 잃지 않는다."""
    full = slugify(title)
    assert slugify(title, max_chars=SLUG_MAX_CHARS) == full, label


def test_long_title_keeps_whole_words_only():
    """잘린 뒤에도 남은 조각은 전부 원래 단어여야 한다."""
    title = "EasyEffects를 모든 Linux 배포판에 포함해 노트북 스피커 음질을 개선해야 함"
    words = set(slugify(title).split("-"))
    assert set(slugify(title, max_chars=SLUG_MAX_CHARS).split("-")) <= words


def test_boundary_exactly_at_limit_keeps_the_whole_word():
    """경계가 딱 `max_chars` 위치면 그 앞 단어는 온전히 들어간다.

    `slug[:max_chars]` 만 보던 옛 코드는 이 하이픈이 시야 밖이라 멀쩡한 단어를
    하나 더 버렸다. 그래서 `max_chars + 1` 까지 들여다본다.
    """
    slug = slugify("alpha beta gamma delta epsilon zeta eta theta", max_chars=39)
    assert slug == "alpha-beta-gamma-delta-epsilon-zeta-eta"


def test_title_without_spaces_is_cut_by_length():
    """하이픈이 없으면 자를 자리가 없다. 길이로 자르되 제한은 지킨다."""
    slug = slugify("코드리뷰자동화도구를" * 6, max_chars=SLUG_MAX_CHARS)
    assert len(slug) == SLUG_MAX_CHARS
    assert "-" not in slug


def test_long_leading_word_is_not_reduced_to_a_stub():
    """첫 단어가 짧아 경계가 맨 앞에 있으면, 경계를 포기하고 길이로 자른다.

    `ai` 만 남기면 파일명이 서로를 구분하지 못한다 — 구분자 역할조차 못 한다.
    """
    slug = slugify("ai " + "가나다라마바사" * 8, max_chars=SLUG_MAX_CHARS)
    assert len(slug) == SLUG_MAX_CHARS
    assert slug.startswith("ai-")


def test_empty_title_falls_back():
    assert build_filename(title="!!!", source_name="!!!", processed_at=PROCESSED_AT) == (
        "2026-08-29-unknown-untitled.md"
    )


def test_sanitize_filename_never_returns_empty():
    assert sanitize_filename('*?"') == "untitled"


def test_filename_format(context):
    name = build_filename(
        title=context.item.title,
        source_name=context.item.source_name,
        processed_at=PROCESSED_AT,
    )
    assert name.startswith("2026-08-29-geeknews-")
    assert name.endswith(".md")


# ---------------------------------------------------------------------------
# writer — 충돌 처리
# ---------------------------------------------------------------------------
def test_writes_file_into_tmp_path(tmp_path, ontology, context):
    result = write_note(ontology, context, output_dir=tmp_path)
    assert result.written is True
    assert result.path.exists()
    assert result.path.parent == tmp_path
    text = result.path.read_text(encoding="utf-8")
    assert text.startswith("---\n")
    assert "## 요약" in text


def test_same_article_is_skipped_by_default(tmp_path, ontology, context):
    first = write_note(ontology, context, output_dir=tmp_path)
    second = write_note(ontology, context, output_dir=tmp_path)

    assert second.written is False
    assert second.reason == "already_exists"
    assert second.path == first.path
    assert len(list(tmp_path.glob("*.md"))) == 1


def test_same_article_overwrites_when_asked(tmp_path, ontology, context):
    first = write_note(ontology, context, output_dir=tmp_path)
    # 본문만 손대고 frontmatter 는 남긴다 — source_url 이 있어야 같은 기사로 인식된다.
    original = first.path.read_text(encoding="utf-8")
    first.path.write_text(original.replace("## 요약", "## 손으로 고친 제목"), encoding="utf-8")

    second = write_note(ontology, context, output_dir=tmp_path, on_conflict="overwrite")
    assert second.written is True
    assert second.path == first.path
    assert second.reason == "overwritten"
    assert "## 요약" in second.path.read_text(encoding="utf-8")
    assert len(list(tmp_path.glob("*.md"))) == 1


def test_overwrite_needs_intact_frontmatter(tmp_path, ontology, context):
    """frontmatter 가 사라진 노트는 같은 기사로 인식할 수 없어 새 파일이 된다.

    `source_url` 로만 동일성을 판정하기 때문이다. 사용자가 frontmatter 를 통째로
    지우면 재실행 시 노트가 하나 더 생긴다 — 알려진 한계다.
    """
    first = write_note(ontology, context, output_dir=tmp_path)
    first.path.write_text("frontmatter 를 지운 노트", encoding="utf-8")

    second = write_note(ontology, context, output_dir=tmp_path, on_conflict="overwrite")
    assert second.path != first.path
    assert second.path.stem.endswith("-2")


def test_different_article_with_same_slug_gets_suffix(tmp_path, ontology, item, context):
    first = write_note(ontology, context, output_dir=tmp_path)

    other = item.model_copy(update={"url": "https://news.hada.io/topic?id=99999"})
    second = write_note(
        ontology,
        NoteContext(item=other, processed_at=PROCESSED_AT),
        output_dir=tmp_path,
    )

    assert second.written is True
    assert second.path != first.path
    assert second.path.stem.endswith("-2")
    assert len(list(tmp_path.glob("*.md"))) == 2


def test_third_collision_gets_suffix_3(tmp_path, ontology, item, context):
    write_note(ontology, context, output_dir=tmp_path)
    for n, url in enumerate(
        ["https://news.hada.io/topic?id=1", "https://news.hada.io/topic?id=2"], start=2
    ):
        result = write_note(
            ontology,
            NoteContext(item=item.model_copy(update={"url": url}), processed_at=PROCESSED_AT),
            output_dir=tmp_path,
        )
        assert result.path.stem.endswith(f"-{n}")


def test_resolve_path_matches_by_source_url(tmp_path):
    path = tmp_path / "note.md"
    path.write_text("---\ntitle: x\nsource_url: https://a.example/1\n---\n", encoding="utf-8")

    same, is_same = resolve_path(tmp_path, "note.md", source_url="https://a.example/1")
    assert (same, is_same) == (path, True)

    other, is_same = resolve_path(tmp_path, "note.md", source_url="https://b.example/2")
    assert other.name == "note-2.md" and is_same is False


def test_unparseable_existing_note_is_treated_as_different(tmp_path):
    """사용자가 손으로 고쳐 frontmatter 가 깨져도 쓰기가 실패하면 안 된다."""
    (tmp_path / "note.md").write_text("frontmatter 없는 노트", encoding="utf-8")
    path, is_same = resolve_path(tmp_path, "note.md", source_url="https://a.example/1")
    assert path.name == "note-2.md" and is_same is False


def test_unknown_conflict_policy_raises(tmp_path, ontology, context):
    write_note(ontology, context, output_dir=tmp_path)
    with pytest.raises(WriteError):
        write_note(ontology, context, output_dir=tmp_path, on_conflict="nonsense")


def test_version_policy_is_explicitly_unimplemented(tmp_path, ontology, context):
    write_note(ontology, context, output_dir=tmp_path)
    with pytest.raises(WriteError, match="구현되지"):
        write_note(ontology, context, output_dir=tmp_path, on_conflict="version")


def test_creates_missing_directory(tmp_path, ontology, context):
    target = tmp_path / "없던" / "폴더"
    result = write_note(ontology, context, output_dir=target)
    assert result.path.exists()


# ---------------------------------------------------------------------------
# writer — 원자적 쓰기
# ---------------------------------------------------------------------------
def test_atomic_write_leaves_no_temp_file(tmp_path):
    target = tmp_path / "note.md"
    atomic_write(target, "내용")
    assert target.read_text(encoding="utf-8") == "내용"
    assert list(tmp_path.glob(".tmp-*")) == []


def test_atomic_write_cleans_up_on_failure(tmp_path, monkeypatch):
    """rename 직전에 죽어도 임시 파일이 남지 않고 원본도 안 망가진다."""
    target = tmp_path / "note.md"
    target.write_text("원래 내용", encoding="utf-8")

    def boom(*args, **kwargs):
        raise OSError("디스크 폭발")

    monkeypatch.setattr("obsidian_writer.writer.os.replace", boom)
    with pytest.raises(OSError):
        atomic_write(target, "새 내용")

    assert target.read_text(encoding="utf-8") == "원래 내용"  # 부분 파일 없음
    assert list(tmp_path.glob(".tmp-*")) == []                # 임시 파일 정리됨


def test_atomic_write_uses_same_directory(tmp_path, monkeypatch):
    """os.replace 는 같은 볼륨에서만 원자적이라 임시 파일도 같은 디렉터리여야 한다."""
    seen: dict[str, object] = {}
    real_mkstemp = __import__("tempfile").mkstemp

    def spy(*args, **kwargs):
        seen["dir"] = kwargs.get("dir")
        return real_mkstemp(*args, **kwargs)

    monkeypatch.setattr("obsidian_writer.writer.tempfile.mkstemp", spy)
    target = tmp_path / "sub" / "note.md"
    atomic_write(target, "내용")
    assert Path(seen["dir"]) == target.parent


def test_note_is_written_with_lf_newlines(tmp_path, ontology, context):
    """Windows 에서도 LF 로 쓴다 — Vault 가 git 으로 동기화될 때 diff 가 흔들린다."""
    result = write_note(ontology, context, output_dir=tmp_path)
    assert b"\r\n" not in result.path.read_bytes()
