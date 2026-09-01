"""프롬프트에 정책이 실제로 반영됐는지 검증.

프롬프트는 코드가 아니라 텍스트라 조용히 썩는다. 결정 로그에 남긴 규칙이
실제 프롬프트 파일에 들어 있는지, 렌더링 후에도 살아남는지 고정해 둔다.

여기서 검증하는 것은 **규칙의 존재**이지 모델이 규칙을 지키는지가 아니다.
후자는 eval/ 의 몫이다.
"""

from __future__ import annotations

from datetime import date

import pytest

from collectors.base import RawItem
from extraction.extractor import DEFAULT_PROMPT, build_variables, load_prompt
from extraction.schema import ReleaseType, TechDomain

V1 = "extract_ontology.v1.md"
V2 = "extract_ontology.v2.md"
V3 = "extract_ontology.v3.md"
V4 = "extract_ontology.v4.md"

#: 규칙 존재 검증은 **활성 버전**을 따라간다. 버전을 올릴 때 이 상수만 바꾸면
#: 되고, 아래 보존 테스트가 과거 버전이 함께 바뀌지 않았는지 잡는다.
ACTIVE = V4


@pytest.fixture
def item() -> RawItem:
    return RawItem(
        url="https://news.hada.io/topic?id=33001",
        title="바이브코딩으로 만든 퍼저가 FFmpeg의 0 나누기 버그를 발견",
        body="FFmpeg의 Sony PS2 VPK 디먹서에서 0 나누기 버그가 발견됨",
        source_name="GeekNews",
        published_at=date(2026, 8, 29),
        collected_at=date(2026, 8, 29),
        tags=("ko", "aggregator"),
    )


# ---------------------------------------------------------------------------
# 버저닝 관례
# ---------------------------------------------------------------------------
def test_v1_is_preserved():
    """v1 은 실제 추출에 쓰였으므로 지우거나 고치지 않는다 (D-010 / D-023)."""
    v1 = load_prompt(V1)
    assert v1.system and v1.user
    # v1 에는 v2 이후 규칙이 없어야 한다. 있으면 v1 을 수정했다는 뜻이다.
    assert "영어 표준형" not in v1.system


def test_v2_is_preserved():
    """v2 도 실제 추출 2건에 쓰였다. `요약` 규칙은 v3 이후에만 있어야 한다 (D-027)."""
    v2 = load_prompt(V2)
    assert "영어 표준형" in v2.system      # v2 의 규칙은 그대로
    assert "### 요약" not in v2.system     # v3 이후의 규칙이 새어들지 않았다


def test_v3_is_preserved():
    """v3 은 골든셋 2번째 항목(S3Gym) 추출에 쓰였다 (D-053).

    v4 에서 넣은 규칙이 v3 으로 새어들면, 그 항목이 어떤 규칙 아래 나온
    출력인지 알 수 없게 된다.
    """
    v3 = load_prompt(V3).system
    assert "### 요약" in v3                      # v3 의 규칙은 그대로
    assert "Partnership/Contract" not in v3      # v4 의 규칙이 새어들지 않았다
    assert "인수 주체" not in v3


def test_default_prompt_is_active_version():
    assert DEFAULT_PROMPT == ACTIVE


# ---------------------------------------------------------------------------
# D-020: 자유 태그 영어 표준형
# ---------------------------------------------------------------------------
def test_active_states_english_first_rule():
    system = load_prompt(ACTIVE).system
    assert "영어 표준형" in system
    assert "병기하지 않는다" in system


@pytest.mark.parametrize("good", ["Fuzzing", "Vibe Coding", "Transformer"])
def test_active_shows_good_examples(good):
    assert good in load_prompt(ACTIVE).system


@pytest.mark.parametrize("bad", ["퍼징(Fuzzing)", "바이브코딩", "트랜스포머"])
def test_active_shows_bad_examples(bad):
    """실제로 나왔던 표기를 반례로 명시해야 한다."""
    assert bad in load_prompt(ACTIVE).system


# ---------------------------------------------------------------------------
# D-022: Agent 판단 기준
# ---------------------------------------------------------------------------
def test_active_narrows_agent_criteria():
    system = load_prompt(ACTIVE).system
    assert "`Agent` 는 좁게 쓴다" in system
    # 실제 오분류 사례를 반례로 담고 있어야 한다.
    assert "퍼저" in system
    assert "보조 도구로 쓰였을 뿐" in system


# ---------------------------------------------------------------------------
# D-021: 발표유형 신규 값
# ---------------------------------------------------------------------------
def test_active_explains_community_discussion_boundary():
    system = load_prompt(ACTIVE).system
    assert "Community/Discussion" in system
    assert "누가 냈는가" in system


def test_release_type_list_injected_includes_new_value(item):
    """어휘는 하드코딩이 아니라 Enum 주입이므로, 신규 값이 자동으로 실린다 (D-012)."""
    system, _ = load_prompt(ACTIVE).render(**build_variables(item))
    for member in ReleaseType:
        assert f"`{member.value}`" in system
    for member in TechDomain:
        assert f"`{member.value}`" in system


# ---------------------------------------------------------------------------
# 렌더링
# ---------------------------------------------------------------------------
def test_active_renders_without_leftover_placeholders(item):
    system, user = load_prompt(ACTIVE).render(**build_variables(item))
    assert "{{" not in system and "}}" not in system
    assert "{{" not in user and "}}" not in user
    assert item.title in user
    assert item.body in user


def test_active_renders_with_empty_body(item):
    """본문 없는 피드(title_only)에서도 빈 자리가 남지 않아야 한다."""
    _, user = load_prompt(ACTIVE).render(**build_variables(item.model_copy(update={"body": ""})))
    assert "{{" not in user
    assert "(정보 없음)" in user


def test_active_keeps_earlier_core_rules():
    """새 규칙을 넣다가 기존 규칙을 떨어뜨리지 않았는지."""
    v1, active = load_prompt(V1).system, load_prompt(ACTIVE).system
    for rule in [
        "주어진 원문에만 근거한다",
        "통제어휘 밖의 값을 만들지 않는다",
        "원문이 잘려 있을 수 있다",
        "정규명",
        "5는 아껴 쓴다",
    ]:
        assert rule in v1, f"전제가 틀렸다: v1 에 '{rule}' 이 없다"
        assert rule in active, f"활성 버전에서 '{rule}' 규칙이 사라졌다"


# ---------------------------------------------------------------------------
# D-027: 요약 필드 (v3 신규)
# ---------------------------------------------------------------------------
def test_active_states_summary_rules():
    """요약과 영향도 근거의 역할이 다르다는 점이 명시돼야 한다 (D-027)."""
    system = load_prompt(ACTIVE).system
    assert "### 요약" in system
    assert "무슨 일이 있었나" in system
    assert "평서문" in system


def test_active_forbids_meta_expressions_in_summary():
    system = load_prompt(ACTIVE).system
    for banned in ["이 기사는", "본문에 따르면", "요약하자면"]:
        assert banned in system, f"금지 표현 '{banned}' 가 예시에 없다"


# ---------------------------------------------------------------------------
# D-056 / D-057 / D-058: v4 신규 규칙
# ---------------------------------------------------------------------------
def test_active_explains_partnership_contract_boundary():
    """새 어휘 값은 Enum 주입으로 목록에 실리지만(D-012), **언제 고르는지는
    프롬프트에만 있다.** 값만 늘리고 기준을 안 주면 쓸 근거가 없다.
    """
    system = load_prompt(ACTIVE).system
    assert "Partnership/Contract" in system
    assert "소유권" in system and "거래 관계" in system


def test_active_distinguishes_partnership_from_product_launch():
    system = load_prompt(ACTIVE).system
    assert "누구에게 무엇이 생겼는가" in system


def test_active_clarifies_who_means_event_subject():
    """D-021 의 '누가' 가 애그리게이터에서 모호했다 (D-057).

    문자 그대로 읽으면 GeekNews 항목이 전부 `Community/Discussion` 이 되어
    그 소스에서 어휘가 무의미해진다.
    """
    system = load_prompt(ACTIVE).system
    assert "링크를 올린 사람이 아니라 사건의 주체" in system


def test_active_lists_acquirer_role():
    """#33003 에서 SpaceX(인수 주체)가 `경쟁사` 로 밀렸다 (D-058)."""
    system = load_prompt(ACTIVE).system
    assert "`인수 주체`" in system
    assert "주는 쪽과 받는 쪽을 구분한다" in system
