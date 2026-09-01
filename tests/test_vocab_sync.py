"""통제어휘 동기화 검증.

`extraction/schema.py` 의 Enum 이 단일 출처(SSoT)이고 `config.yaml: ontology` 는
사람이 읽기 위한 미러다(결정 로그 D-002). 같은 어휘가 두 곳에 적혀 있는 구조라
드리프트를 사람 눈으로 막을 수 없어서, 이 테스트가 그 역할을 대신한다.

어휘를 바꿨는데 이 테스트가 깨졌다면 config.yaml 을 함께 고치라는 뜻이다.
`schema.md` 의 표도 같이 고쳐야 하지만 그건 문서라 자동 검증 대상이 아니다.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from extraction.schema import ReleaseType, TechDomain

CONFIG_PATH = Path(__file__).resolve().parent.parent / "config.yaml"


@pytest.fixture(scope="module")
def ontology_config() -> dict:
    config = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))
    ontology = config.get("ontology")
    assert ontology, "config.yaml 에 ontology 블록이 없습니다"
    return ontology


@pytest.mark.parametrize(
    ("enum_cls", "config_key"),
    [(TechDomain, "tech_domain"), (ReleaseType, "release_type")],
)
def test_vocabulary_matches_config(enum_cls, config_key, ontology_config):
    """Enum 값 집합과 config.yaml 미러가 정확히 일치해야 한다 (순서 포함)."""
    from_code = [member.value for member in enum_cls]
    from_config = ontology_config.get(config_key)

    assert from_config is not None, f"config.yaml: ontology.{config_key} 가 없습니다"

    missing = set(from_code) - set(from_config)
    extra = set(from_config) - set(from_code)
    assert not missing, f"config.yaml 에 빠진 값: {sorted(missing)}"
    assert not extra, f"config.yaml 에만 있는 값: {sorted(extra)}"

    # 순서까지 맞춘다. 문서로서의 미러이므로 읽는 순서가 달라지면 대조가 어렵다.
    assert from_config == from_code, "값은 같지만 순서가 다릅니다"


def test_community_discussion_exists_in_both(ontology_config):
    """D-021 로 추가한 값이 Enum 과 config.yaml 양쪽에 있어야 한다.

    커뮤니티 발 버그 리포트/토론이 `Benchmark/Report` 로 밀려나던 빈 칸을 메운
    값이다. 어느 한쪽에서만 지워지면 오분류가 조용히 돌아온다.
    """
    assert ReleaseType.COMMUNITY.value == "Community/Discussion"
    assert "Community/Discussion" in ontology_config["release_type"]


def test_partnership_contract_exists_in_both(ontology_config):
    """D-056 으로 추가한 값이 Enum 과 config.yaml 양쪽에 있어야 한다.

    "조직 간 거래 관계가 바뀌었다"가 `Funding/M&A` 로 밀려나던 빈 칸을 메운
    값이다(#33003). 어느 한쪽에서만 지워지면 오분류가 조용히 돌아온다.
    """
    assert ReleaseType.PARTNERSHIP.value == "Partnership/Contract"
    assert "Partnership/Contract" in ontology_config["release_type"]


def test_release_type_count_is_pinned(ontology_config):
    """어휘 크기를 고정한다.

    값이 늘거나 줄면 **과거 라벨의 의미가 바뀔 수 있으므로**(D-022) 조용히
    지나가면 안 된다. 이 숫자를 고칠 때는 schema.md 표와 결정 로그도 함께
    고쳤는지 확인할 것.
    """
    assert len(list(ReleaseType)) == 8
    assert len(ontology_config["release_type"]) == 8


def test_release_type_naming_convention():
    """기존 6개 값의 명명 패턴(PascalCase, 구분자는 '/')을 따르는지."""
    for member in ReleaseType:
        value = member.value
        assert " " not in value, f"{value}: 공백을 쓰지 않는다"
        for part in value.split("/"):
            assert part[:1].isupper(), f"{value}: 각 파트는 대문자로 시작한다"


def test_no_duplicate_vocabulary_values():
    """Enum 값에 중복이 없어야 한다 (StrEnum 은 중복을 alias 로 조용히 흡수한다)."""
    for enum_cls in (TechDomain, ReleaseType):
        values = [member.value for member in enum_cls]
        assert len(values) == len(set(values)), f"{enum_cls.__name__} 에 중복 값이 있습니다"


def test_relevance_gate_temperature_is_zero():
    """게이트는 재현 가능해야 한다 (D-032).

    temperature 를 비워 두면 API 기본값(1.0)이 적용돼 같은 기사가 실행마다 다르게
    판정된다. 실제로 #33001 이 5회 중 4통과/1스킵으로 갈렸다.
    """
    config = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))
    gate = (config.get("llm") or {}).get("relevance_gate") or {}
    assert "temperature" in gate, "relevance_gate 에 temperature 항목이 없습니다"
    assert gate["temperature"] == 0


def test_extraction_temperature_is_not_set():
    """Opus 5 는 temperature 를 거부한다(400 'deprecated for this model').

    값이 들어가면 추출 단계가 통째로 실패한다. null 로 남아 있어야 한다.
    """
    config = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))
    extraction = (config.get("llm") or {}).get("extraction") or {}
    assert extraction.get("temperature") is None


def test_temperature_is_only_sent_when_configured():
    """None 이면 요청에 temperature 자체가 실리지 않아야 한다."""
    from extraction.llm import AnthropicClient

    gate = AnthropicClient(model="claude-haiku-4-5-20251001", effort=None, thinking=False,
                           temperature=0, client=object())
    assert gate._request_kwargs()["extra_body"] == {"temperature": 0}

    extract = AnthropicClient(model="claude-opus-5", temperature=None, client=object())
    assert "extra_body" not in extract._request_kwargs()


def test_impact_scale_matches_schema(ontology_config):
    """영향도 척도(1~5)와 근거 필수 여부가 config 미러와 일치해야 한다."""
    from extraction.schema import Impact

    impact_cfg = ontology_config.get("impact") or {}
    assert impact_cfg.get("scale") == [1, 2, 3, 4, 5]
    assert impact_cfg.get("require_rationale") is True

    schema = Impact.model_json_schema()["properties"]
    assert schema["점수"]["minimum"] == 1
    assert schema["점수"]["maximum"] == 5
    # 근거가 required 여야 "점수만 있고 근거 없는" 결과를 스키마가 막아준다.
    assert "근거" in Impact.model_json_schema()["required"]
