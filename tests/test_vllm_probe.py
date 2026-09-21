"""반증 probe (`extraction/probe.py`) — **판별 방향이 뒤집히지 않는가.**

이 파일이 지키는 것은 하나다. session-03 의 1차 probe 는 "스키마를 어겨 보라"로
짜여 있었고, 그래서 **강제가 켜져 있다는 증거가 꺼져 있다는 증거로 읽혔다.**
설계를 모듈로 고정했으니, 그 설계가 의도한 방향으로 판정하는지를 여기서 잰다.

| 응답 | 기대 판정 |
|---|---|
| 스키마에 맞는 JSON | **강제됨** — 모델은 산문을 쓰라는 지시를 따르려 했는데 나갈 수 없었다 |
| 산문 | **강제 아님** — 모델이 지시를 따른 것뿐이다 |

**네트워크로 나가지 않는다.**
"""

from __future__ import annotations

import json
from typing import Any

from extraction.llm import SchemaMismatchError, StructuredResult, Usage
from extraction.probe import PROBE_SYSTEM, ProbeAnswer, run_probe


def _raw(content: str, finish_reason: str = "stop") -> dict[str, Any]:
    return {
        "choices": [{"message": {"content": content}, "finish_reason": finish_reason}],
        "usage": {"prompt_tokens": 60, "completion_tokens": 8},
    }


class _Client:
    """`parse_into` 하나만 있는 최소 대역."""

    model = "gemma-4-31B-it"

    def __init__(self, outcome: Any, raws: list[dict[str, Any]]):
        self._outcome = outcome
        self.last_raw_responses: list[dict[str, Any]] = []
        self._raws = raws

    def parse_into(self, *, system, user, output_model):
        self.last_raw_responses = list(self._raws)
        if isinstance(self._outcome, BaseException):
            raise self._outcome
        return self._outcome


class TestTheVerdictPointsTheRightWay:
    def test_schema_shaped_json_means_enforced(self):
        """모델은 '산문으로 답하라'는 지시를 받았다. JSON 이 나왔다면 **못 나간 것**이다."""
        body = json.dumps({"답": "예"}, ensure_ascii=False)
        client = _Client(
            StructuredResult(value=ProbeAnswer(답="예"), usage=Usage(), attempts=1),
            [_raw(body)],
        )
        result = run_probe(client)
        assert result.enforced is True
        assert result.verdict == "강제됨"
        assert result.calls == 1

    def test_prose_means_not_enforced(self):
        """산문이 나왔다면 모델이 지시를 따른 것이고, 디코더는 아무것도 막지 않았다."""
        prose = "물은 상온에서 액체입니다. 1기압 기준 0도에서 100도 사이에서 그렇습니다."
        client = _Client(
            SchemaMismatchError("검증 실패", attempts=2, last_error="not json"),
            [_raw(prose), _raw(prose)],
        )
        result = run_probe(client)
        assert result.enforced is False
        assert result.verdict == "강제 아님"
        # 재시도가 붙어 2건이다 — 승인 상한 2건이 여기서 나온다.
        assert result.calls == 2


class TestTheProbeKeepsItsShape:
    def test_the_prompt_tells_the_model_not_to_use_json(self):
        """이 문구가 바뀌면 판별의 의미가 바뀐다.

        "스키마를 어겨 보라"로 돌아가면 모델의 순종도와 디코더의 강제가 다시
        섞인다 — 그게 session-03 에서 정반대 결론을 만든 원인이다.
        """
        assert "JSON 을 쓰지 마라" in PROBE_SYSTEM
        assert "중괄호" in PROBE_SYSTEM

    def test_the_schema_is_a_closed_enum(self):
        """열린 문자열이면 산문도 스키마를 통과해 판별이 성립하지 않는다."""
        schema = ProbeAnswer.model_json_schema(by_alias=True)
        answer = schema["properties"]["답"]
        assert answer.get("enum") == ["예", "아니오"]
        assert schema.get("required") == ["답"]

    def test_finish_reasons_are_carried(self):
        """절단(`length`)과 판별 실패를 같은 칸에 넣지 않기 위해 필요하다."""
        client = _Client(
            SchemaMismatchError("실패", attempts=1, last_error="x"),
            [_raw("잘린 응답", finish_reason="length")],
        )
        assert run_probe(client).finish_reasons == ("length",)
