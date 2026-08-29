"""기업명 정규화 단위 테스트.

핵심 계약 두 가지:
  1. 사전에 **명시적으로 등록된** 표기만 정규화된다.
  2. 유사하지만 미등록인 표기는 **추측하지 않고** 원문을 승계한다 (D-025).
"""

from __future__ import annotations

import pytest
import yaml

from extraction.normalize import (
    DEFAULT_CONFIG_PATH,
    build_alias_index,
    load_alias_index,
    normalize_company,
    normalization_key,
)
from extraction.schema import CompanyRef


@pytest.fixture(scope="module")
def config_aliases() -> dict:
    config = yaml.safe_load(DEFAULT_CONFIG_PATH.read_text(encoding="utf-8"))
    aliases = config.get("company_aliases")
    assert aliases, "config.yaml 에 company_aliases 가 없습니다"
    return aliases


# ---------------------------------------------------------------------------
# a) 등록된 이름이 정확히 매칭된다 — config.yaml 실제 값 사용
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("OpenAI", "OpenAI"),          # 대표명 그대로
        ("오픈AI", "OpenAI"),           # 한국어 별칭
        ("오픈에이아이", "OpenAI"),
        ("앤트로픽", "Anthropic"),
        ("Anthropic", "Anthropic"),
        ("딥마인드", "Google DeepMind"),
        ("엔비디아", "NVIDIA"),
        ("허깅페이스", "Hugging Face"),
        ("facebook", "Meta"),          # 구 사명도 대표명으로 모인다
    ],
)
def test_registered_aliases_resolve(raw, expected):
    result = normalize_company(raw)
    assert result.canonical == expected
    assert result.resolved is True


def test_every_config_alias_resolves(config_aliases):
    """사전에 적힌 모든 표기가 실제로 매칭돼야 한다.

    오타나 잘못된 들여쓰기로 별칭이 죽어 있으면 여기서 잡힌다.
    """
    for canonical, aliases in config_aliases.items():
        assert normalize_company(canonical).canonical == canonical
        for alias in aliases:
            result = normalize_company(alias)
            assert result.resolved is True, f"{alias!r} 가 매칭되지 않습니다"
            assert result.canonical == canonical, f"{alias!r} -> {result.canonical!r}"


# ---------------------------------------------------------------------------
# b) 미등록 이름은 원문을 승계한다
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("raw", ["FFmpeg", "Cursor", "SpaceX", "듣도보도못한회사"])
def test_unregistered_names_fall_back_to_raw(raw):
    result = normalize_company(raw)
    assert result.canonical == raw
    assert result.resolved is False


def test_empty_string_is_not_resolved():
    result = normalize_company("")
    assert result.canonical == ""
    assert result.resolved is False


# ---------------------------------------------------------------------------
# c) 대소문자 / 공백 차이는 흡수한다
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "raw",
    ["openai", "OPENAI", "OpenAi", "Open AI", "open  ai", " OpenAI ", "Open\tAI"],
)
def test_case_and_whitespace_are_absorbed(raw):
    result = normalize_company(raw)
    assert result.canonical == "OpenAI"
    assert result.resolved is True


def test_nbsp_is_absorbed():
    """피드 본문에서 넘어오는 비파괴 공백(\\xa0)도 공백으로 본다."""
    assert normalize_company("Open\xa0AI").canonical == "OpenAI"


def test_normalization_key_only_folds_case_and_space():
    assert normalization_key("Open AI") == normalization_key("openai")
    # 하이픈·점은 지우지 않는다 — 지우기 시작하면 기준이 사라진다.
    assert normalization_key("Open-AI") != normalization_key("openai")


# ---------------------------------------------------------------------------
# d) 퍼지 매칭이 일어나지 않는다 (D-025)
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "raw",
    [
        "OpenAI Korea",   # 등록명을 포함하지만 다른 조직
        "OpenAI Japan",
        "Open",           # 부분 문자열
        "openal",         # 한 글자 차이 (오타 유사)
        "Meta Platforms", # 등록명(Meta)의 확장형
        "NVIDIA Research",
        "앤트로",          # 별칭의 접두사
        "구글",            # 'Google DeepMind' 의 일부지만 별개
    ],
)
def test_no_fuzzy_matching(raw):
    """유사하지만 사전에 없는 이름은 정규화되지 않고 그대로 남아야 한다.

    잘못된 정규화는 서로 다른 기업을 같은 그래프 노드로 합쳐 버리고,
    합쳐진 뒤에는 원래 무엇이었는지 복구할 수 없다.
    """
    result = normalize_company(raw)
    assert result.resolved is False, f"{raw!r} 가 잘못 매칭됐습니다 -> {result.canonical!r}"
    assert result.canonical == raw


# ---------------------------------------------------------------------------
# 인덱스 생성
# ---------------------------------------------------------------------------
def test_build_index_includes_canonical_itself():
    index = build_alias_index({"Acme": ["acme corp"]})
    assert index[normalization_key("Acme")] == "Acme"
    assert index[normalization_key("acme corp")] == "Acme"


def test_build_index_keeps_first_on_conflict(capsys):
    """같은 표기가 두 대표명에 등록되면 경고하고 먼저 등록된 쪽을 유지한다."""
    index = build_alias_index({"First": ["shared"], "Second": ["shared"]})
    assert index[normalization_key("shared")] == "First"
    assert "충돌" in capsys.readouterr().err


def test_missing_config_does_not_raise(tmp_path, capsys):
    """설정을 못 읽어도 예외 대신 빈 사전으로 동작한다.

    이 함수는 pydantic 검증 경로에서 호출된다. 여기서 던지면 설정 파일 문제가
    스키마 검증 실패로 둔갑한다.
    """
    index = load_alias_index(tmp_path / "없는파일.yaml")
    assert index == {}
    assert "로드 실패" in capsys.readouterr().err


def test_injected_index_overrides_config():
    result = normalize_company("acme", index={normalization_key("acme"): "Acme Inc"})
    assert result == type(result)(canonical="Acme Inc", resolved=True)


# ---------------------------------------------------------------------------
# CompanyRef 통합 — 정규화가 스키마 검증 시점에 실제로 걸리는가
# ---------------------------------------------------------------------------
def test_company_ref_normalizes_on_validate():
    company = CompanyRef.model_validate({"원문표기": "오픈AI", "역할": "발표 주체"})
    assert company.raw == "오픈AI"          # 원문은 보존
    assert company.canonical == "OpenAI"    # 대표명으로 정규화
    assert company.resolved is True
    assert company.role == "발표 주체"


def test_company_ref_keeps_raw_when_unregistered():
    company = CompanyRef.model_validate({"원문표기": "FFmpeg"})
    assert company.canonical == "FFmpeg"
    assert company.resolved is False


def test_company_ref_overwrites_supplied_values():
    """LLM 이 정규명을 보내와도 사전 결과가 이긴다 (사전이 단일 출처)."""
    company = CompanyRef.model_validate(
        {"원문표기": "오픈AI", "정규명": "엉뚱한값", "정규화성공": False}
    )
    assert company.canonical == "OpenAI"
    assert company.resolved is True


def test_company_ref_does_not_invent_resolution():
    company = CompanyRef.model_validate(
        {"원문표기": "OpenAI Korea", "정규명": "OpenAI", "정규화성공": True}
    )
    assert company.canonical == "OpenAI Korea"
    assert company.resolved is False
