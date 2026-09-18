"""`VLLMClient` 단위 테스트 (실제 호출 없음) — ADR-018 / ADR-019.

전부 `transport` 주입으로 돈다. **엔드포인트가 필요한 검사를 CI 계약에 넣지 않는다** —
엔드포인트는 관리형이고 할당이 끝나면 사라지므로(MARA ADR-003), 그것에 의존하는
테스트는 레포와 무관한 이유로 빨개진다.

⚠️ **강제 여부(ADR-019)는 여기서 검사할 수 없다.** 그건 서버의 성질이고 실제 호출로만
확인된다. 절차는 `.claude/rules/vllm-endpoint.md` 에 있고, 이 파일은 **우리 쪽 요청이
강제를 요구하는 형태인지**까지만 고정한다.
"""

from __future__ import annotations

import json

import pytest

from extraction.llm import SchemaMismatchError, client_from_config
from extraction.schema import NewsOntology, RelevanceGate
from extraction.vllm import VLLMClient, VLLMEndpointError

GOOD_GATE = {
    "관련있음": True,
    "근거": "AI 엔지니어링과 직접 관련된 항목이라 통과시킨다.",
}


def response(body: object, *, finish: str = "stop") -> dict:
    text = body if isinstance(body, str) else json.dumps(body, ensure_ascii=False)
    return {
        "model": "gemma-4-31B-it",
        "choices": [{"finish_reason": finish, "message": {"content": text}}],
        "usage": {"prompt_tokens": 100, "completion_tokens": 40},
    }


def scripted(*responses: dict):
    """호출 순서대로 돌려주는 transport. 보낸 payload 도 모아 둔다."""
    sent: list[dict] = []
    queue = list(responses)

    def transport(url: str, payload: dict, timeout: float) -> dict:
        sent.append(payload)
        return queue.pop(0)

    transport.sent = sent  # type: ignore[attr-defined]
    return transport


def client(*responses: dict, **kwargs) -> VLLMClient:
    return VLLMClient(transport=scripted(*responses), **kwargs)


# ---------------------------------------------------------------------------
# 요청 형태
# ---------------------------------------------------------------------------
def test_request_asks_for_schema_enforcement():
    """`response_format=json_schema` 로만 강제를 건다 (ADR-019).

    `guided_json` 은 이 엔드포인트에서 **조용히 무시된다.** 이름 하나가 강제 전체를
    없애고 HTTP 200 이 오므로, 요청 형태를 테스트로 못 박는다.
    """
    c = client(response(GOOD_GATE))
    c.parse_into(system="s", user="u", output_model=RelevanceGate)

    payload = c._transport.sent[0]
    fmt = payload["response_format"]
    assert fmt["type"] == "json_schema"
    assert fmt["json_schema"]["strict"] is True
    assert fmt["json_schema"]["name"] == "RelevanceGate"
    assert "guided_json" not in payload
    assert "structured_outputs" not in payload


def test_schema_uses_korean_aliases():
    """스키마 키는 한국어 alias 다 (D-003).

    영어 필드명으로 강제하면 프롬프트와 스키마가 서로 다른 키를 말하게 된다.
    """
    schema = VLLMClient.response_format_for(NewsOntology)["json_schema"]["schema"]
    assert set(schema["properties"]) >= {"요약", "기술영역", "발표유형", "영향도"}


def test_enum_values_reach_the_schema():
    """통제어휘가 스키마 enum 으로 실려야 디코딩이 막을 수 있다."""
    fmt = VLLMClient.response_format_for(NewsOntology)["json_schema"]["schema"]
    blob = json.dumps(fmt, ensure_ascii=False)
    assert "Partnership/Contract" in blob
    assert "Embodied/Robotics" in blob


def test_system_role_is_a_separate_message_by_default():
    c = client(response(GOOD_GATE))
    c.parse_into(system="시스템", user="사용자", output_model=RelevanceGate)
    messages = c._transport.sent[0]["messages"]
    assert [m["role"] for m in messages] == ["system", "user"]


def test_system_as_user_merges_without_touching_the_prompt():
    """채팅 템플릿이 system 을 거부할 때만 켠다. **프롬프트 파일은 안 바뀐다.**"""
    c = client(response(GOOD_GATE), system_as_user=True)
    c.parse_into(system="시스템", user="사용자", output_model=RelevanceGate)
    messages = c._transport.sent[0]["messages"]
    assert [m["role"] for m in messages] == ["user"]
    assert "시스템" in messages[0]["content"] and "사용자" in messages[0]["content"]


def test_temperature_is_omitted_when_none():
    c = client(response(GOOD_GATE), temperature=None)
    c.parse_into(system="s", user="u", output_model=RelevanceGate)
    assert "temperature" not in c._transport.sent[0]


# ---------------------------------------------------------------------------
# 재시도 — 벤더 쪽과 같은 정책이어야 대조가 성립한다 (사전 등록 §2.1-3)
# ---------------------------------------------------------------------------
def test_retries_once_with_the_error_fed_back():
    c = client(response("이건 JSON 이 아니다"), response(GOOD_GATE))
    result = c.parse_into(system="s", user="u", output_model=RelevanceGate)

    assert result.attempts == 2
    retry_messages = c._transport.sent[1]["messages"]
    assert [m["role"] for m in retry_messages] == ["system", "user", "assistant", "user"]
    assert "스키마" in retry_messages[-1]["content"]


def test_two_failures_raise_schema_mismatch():
    c = client(response("깨진 응답"), response("여전히 깨졌다"))
    with pytest.raises(SchemaMismatchError) as exc:
        c.parse_into(system="s", user="u", output_model=RelevanceGate)
    assert exc.value.attempts == 2
    assert exc.value.last_error


def test_partial_result_is_not_returned():
    """부분 통과를 통과시키면 Vault 가 조용히 오염된다 (`SchemaMismatchError` docstring)."""
    partial = {"관련있음": True}  # `근거` 없음
    c = client(response(partial), response(partial))
    with pytest.raises(SchemaMismatchError):
        c.parse_into(system="s", user="u", output_model=RelevanceGate)


def test_raw_responses_are_kept_for_the_caller():
    """변환 전에 원본을 디스크로 내릴 수 있어야 한다 (D-052)."""
    c = client(response("깨짐"), response(GOOD_GATE))
    c.parse_into(system="s", user="u", output_model=RelevanceGate)
    assert len(c.last_raw_responses) == 2
    assert c.last_raw_responses[-1]["choices"][0]["finish_reason"] == "stop"


def test_usage_leaves_unmeasurable_fields_as_none():
    """vLLM 응답에 없는 개념을 0 으로 적으면 "재지 못한 것"이 "0 이었던 것"이 된다."""
    c = client(response(GOOD_GATE))
    usage = c.parse_into(system="s", user="u", output_model=RelevanceGate).usage
    assert usage.input_tokens == 100 and usage.output_tokens == 40
    assert usage.thinking_tokens is None
    assert usage.cache_read_input_tokens is None


def test_non_openai_shape_is_an_error_not_a_silent_empty():
    c = client({"unexpected": "shape"})
    with pytest.raises(VLLMEndpointError):
        c.parse_into(system="s", user="u", output_model=RelevanceGate)


# ---------------------------------------------------------------------------
# 목적지
# ---------------------------------------------------------------------------
def test_vendor_base_url_is_refused():
    """`VLLM_BASE` 를 벤더로 돌리는 경로를 막는다 — 이전 뒤 게이트의 무게가 여기다."""
    from extraction.egress import ExternalEndpointError

    with pytest.raises(ExternalEndpointError):
        VLLMClient(base_url="https://api.anthropic.com/v1")


def test_missing_base_url_stops_instead_of_defaulting(monkeypatch, tmp_path):
    """기본 엔드포인트를 코드에 두지 않는다 (URL 자체가 자격증명, MARA ADR-010)."""
    monkeypatch.delenv("VLLM_BASE", raising=False)
    # `.env` 가 없는 루트를 가리킨다. 레포의 `.env` 를 읽으면 이 검사가 무의미해진다.
    with pytest.raises(VLLMEndpointError):
        VLLMClient(project_root=tmp_path)


# ---------------------------------------------------------------------------
# 팩토리
# ---------------------------------------------------------------------------
def test_factory_builds_a_vllm_client_per_stage(monkeypatch):
    monkeypatch.setenv("VLLM_BASE", "https://vllm.internal.invalid/v1")
    config = {
        "llm": {
            "provider": "vllm",
            "extraction": {"model": "gemma-4-31B-it", "max_tokens": 4321, "temperature": 0},
        }
    }
    c = client_from_config(config, stage="extraction")
    assert isinstance(c, VLLMClient)
    assert (c.model, c.max_tokens, c.temperature, c.stage) == (
        "gemma-4-31B-it",
        4321,
        0,
        "extraction",
    )


def test_factory_drops_vendor_only_parameters(monkeypatch):
    """`effort` / `thinking` 은 gemma 에 대응 개념이 없다. 보내지 않는다."""
    monkeypatch.setenv("VLLM_BASE", "https://vllm.internal.invalid/v1")
    config = {
        "llm": {
            "provider": "vllm",
            "extraction": {"model": "gemma-4-31B-it", "effort": "high", "thinking": "adaptive"},
        }
    }
    c = client_from_config(config, stage="extraction")
    assert not hasattr(c, "effort")
    assert not hasattr(c, "thinking")


def test_factory_rejects_an_unknown_provider():
    from extraction.llm import LLMError

    with pytest.raises(LLMError):
        client_from_config({"llm": {"provider": "openai"}}, stage="extraction")
