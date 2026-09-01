"""extractor / AnthropicClient 단위 테스트 (실제 API 호출 없음).

두 층을 나눠 검증한다.
  1. `extract_ontology` 배선 — 프롬프트 조립, 어휘 주입, 결과 포장
     (LLMClient Protocol 을 만족하는 가짜 클라이언트 주입)
  2. `AnthropicClient.parse_into` 의 검증/재시도 로직
     (anthropic SDK 자리에 가짜 객체 주입 — 네트워크를 타지 않는다)
"""

from __future__ import annotations

import json
from datetime import date
from types import SimpleNamespace

import pytest

from collectors.base import RawItem
from extraction.extractor import DEFAULT_PROMPT, build_variables, extract_ontology, load_prompt
from extraction.llm import AnthropicClient, LLMClient, SchemaMismatchError, StructuredResult, Usage
from extraction.schema import NewsOntology, ReleaseType, TechDomain

# ---------------------------------------------------------------------------
# 픽스처
# ---------------------------------------------------------------------------
VALID_PAYLOAD = {
    "요약": "LLM 보조로 만든 퍼저가 오픈소스 프로젝트에서 버그를 찾아낸 사례다. 재현 조건과 원인 코드가 함께 공개됐다.",
    "기술영역": ["LLM", "Reasoning"],
    "발표유형": "Paper",
    "관련기업": [{"원문표기": "오픈AI", "역할": "발표 주체"}],
    "관련기존기술": ["Transformer", "Chain-of-Thought"],
    "영향도": {
        "점수": 3,
        "근거": "추론 단계 비용을 줄여 다수 팀의 서빙 구성 선택에 영향을 줄 수 있다.",
    },
}


@pytest.fixture
def item() -> RawItem:
    return RawItem(
        url="https://arxiv.org/abs/2608.00001",
        title="Test-Time Scaling without Repeated Sampling",
        body="We introduce a method that improves reasoning without repeated generation.",
        source_name="arXiv cs.CL (Atom API)",
        published_at=date(2026, 8, 27),
        collected_at=date(2026, 8, 29),
        tags=("paper",),
    )


class FakeLLMClient:
    """LLMClient Protocol 을 만족하는 가짜. 호출 인자를 기록한다."""

    def __init__(self, payload: dict) -> None:
        self.payload = payload
        self.calls: list[dict] = []

    def parse_into(self, *, system, user, output_model):
        self.calls.append({"system": system, "user": user, "output_model": output_model})
        return StructuredResult(
            value=output_model.model_validate(self.payload),
            usage=Usage(input_tokens=1200, output_tokens=300, model="fake-model"),
        )


def _sdk_response(*, parsed=None, text: str | None = None):
    """anthropic SDK 응답을 흉내 낸 최소 객체."""
    content = []
    if text is not None:
        content.append(SimpleNamespace(type="text", text=text))
    return SimpleNamespace(
        parsed_output=parsed,
        content=content,
        usage=SimpleNamespace(input_tokens=100, output_tokens=50),
        model="claude-opus-5",
    )


class FakeMessages:
    def __init__(self, responses: list) -> None:
        self._responses = list(responses)
        self.calls: list[dict] = []

    def parse(self, **kwargs):
        self.calls.append(kwargs)
        return self._responses.pop(0)


def fake_sdk(responses: list) -> SimpleNamespace:
    return SimpleNamespace(messages=FakeMessages(responses))


# ---------------------------------------------------------------------------
# 1. extract_ontology 배선
# ---------------------------------------------------------------------------
def test_extract_ontology_returns_validated_model(item):
    client = FakeLLMClient(VALID_PAYLOAD)
    result = extract_ontology(item, client)

    assert isinstance(result.ontology, NewsOntology)
    assert result.ontology.tech_domains == [TechDomain.LLM, TechDomain.REASONING]
    assert result.ontology.release_type is ReleaseType.PAPER
    assert result.ontology.impact.score == 3
    assert result.prompt_name == DEFAULT_PROMPT   # 버전이 아니라 "기록된 이름 == 실제 사용본"을 고정한다
    assert result.usage.input_tokens == 1200


def test_fake_client_satisfies_protocol():
    assert isinstance(FakeLLMClient(VALID_PAYLOAD), LLMClient)


def test_prompt_receives_vocabulary_from_enums(item):
    """통제어휘는 프롬프트에 하드코딩되지 않고 Enum 에서 주입된다 (D-012)."""
    client = FakeLLMClient(VALID_PAYLOAD)
    extract_ontology(item, client)

    system = client.calls[0]["system"]
    for member in TechDomain:
        assert f"`{member.value}`" in system
    for member in ReleaseType:
        assert f"`{member.value}`" in system


def test_prompt_has_no_unsubstituted_placeholders(item):
    client = FakeLLMClient(VALID_PAYLOAD)
    extract_ontology(item, client)

    call = client.calls[0]
    assert "{{" not in call["system"]
    assert "{{" not in call["user"]
    assert item.title in call["user"]
    assert item.body in call["user"]


def test_missing_body_becomes_explicit_placeholder(item):
    """본문이 없는 피드(title_only)에서도 빈 자리가 남지 않아야 한다."""
    variables = build_variables(item.model_copy(update={"body": ""}))
    system, user = load_prompt().render(**variables)
    assert "{{" not in user
    assert "(정보 없음)" in user


# ---------------------------------------------------------------------------
# 2. 스키마 검증 / 재시도
# ---------------------------------------------------------------------------
def test_parse_into_returns_parsed_output():
    parsed = NewsOntology.model_validate(VALID_PAYLOAD)
    client = AnthropicClient(client=fake_sdk([_sdk_response(parsed=parsed)]))

    result = client.parse_into(system="s", user="u", output_model=NewsOntology)

    assert result.value is parsed
    assert result.attempts == 1
    assert result.usage.output_tokens == 50


def test_parse_into_retries_once_then_succeeds():
    """1차 실패 시 오류를 되먹여 재시도하고, 2차 성공을 그대로 돌려준다."""
    bad = _sdk_response(parsed=None, text=json.dumps({"기술영역": ["존재하지않는영역"]}))
    good = _sdk_response(parsed=NewsOntology.model_validate(VALID_PAYLOAD))
    sdk = fake_sdk([bad, good])
    client = AnthropicClient(client=sdk)

    result = client.parse_into(system="s", user="u", output_model=NewsOntology)

    assert result.attempts == 2
    assert len(sdk.messages.calls) == 2
    # 2차 요청에는 1차 응답과 검증 오류가 함께 실려 있어야 한다.
    retry_messages = sdk.messages.calls[1]["messages"]
    assert len(retry_messages) == 3
    assert retry_messages[1]["role"] == "assistant"
    assert "존재하지않는영역" in retry_messages[2]["content"]


def test_parse_into_raises_after_two_failures():
    """2회 실패하면 부분 결과를 반환하지 않고 예외를 올린다."""
    bad = _sdk_response(parsed=None, text='{"기술영역": []}')
    client = AnthropicClient(client=fake_sdk([bad, bad]))

    with pytest.raises(SchemaMismatchError) as exc_info:
        client.parse_into(system="s", user="u", output_model=NewsOntology)

    assert exc_info.value.attempts == 2
    assert exc_info.value.last_error


# ---------------------------------------------------------------------------
# 3. 스키마 계약
# ---------------------------------------------------------------------------
def test_controlled_vocabulary_rejects_unknown_value():
    payload = {**VALID_PAYLOAD, "기술영역": ["Blockchain"]}
    with pytest.raises(Exception):
        NewsOntology.model_validate(payload)


def test_impact_rationale_is_required():
    payload = {**VALID_PAYLOAD, "영향도": {"점수": 5}}
    with pytest.raises(Exception):
        NewsOntology.model_validate(payload)


def test_tech_domains_capped_at_three():
    payload = {
        **VALID_PAYLOAD,
        "기술영역": ["LLM", "RAG", "Agent", "Reasoning"],
    }
    with pytest.raises(Exception):
        NewsOntology.model_validate(payload)


def test_company_is_normalized_but_raw_is_preserved():
    """정규화는 normalize.py 가 하고, 원문 표기는 그대로 보존된다 (D-011 / D-025).

    이전에는 canonical 이 raw 를 그대로 승계했다. normalize.py 구현 이후로는
    사전에 등록된 표기가 대표명으로 정규화된다.
    """
    ontology = NewsOntology.model_validate(VALID_PAYLOAD)
    company = ontology.companies[0]
    assert company.raw == "오픈AI"        # 원문 보존
    assert company.canonical == "OpenAI"  # 사전으로 정규화
    assert company.resolved is True


def test_unregistered_company_still_falls_back_to_raw():
    """미등록 엔티티는 원문을 승계하고 resolved=False 로 남는다."""
    payload = {**VALID_PAYLOAD, "관련기업": [{"원문표기": "FFmpeg", "역할": "영향 대상"}]}
    company = NewsOntology.model_validate(payload).companies[0]
    assert company.canonical == "FFmpeg"
    assert company.resolved is False
