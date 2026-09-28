"""`length` 를 절단과 퇴화로 가른다 (D-093).

session-13 의 v4 judge 는 `{"faithfulness":` 뒤에 `"\\n  "` 를 1,996번 내고 4000 토큰에서
끊겼다. 이전 분류로는 "절단"이었고, 절단의 처방(`max_tokens` 상향)은 퇴화를 **더 늦게
실패하게 만들 뿐**이다. 처방이 정반대인 둘을 한 칸에 세지 않는다.
"""

from __future__ import annotations

from extraction.vllm import DEGENERATE_TAIL_CHARS, count_length_stops, stop_kind


def _resp(content: str, finish: str) -> dict:
    return {"choices": [{"message": {"content": content}, "finish_reason": finish}]}


# session-13 진단 응답의 모양 그대로 (내용은 줄였다)
SESSION13_DEGENERATE = '{\n  "faithfulness":' + "\n  " * 1996


def test_session13_whitespace_loop_is_degenerate():
    assert stop_kind(_resp(SESSION13_DEGENERATE, "length")) == "degenerate"


def test_real_content_cut_is_truncated():
    content = '{"faithfulness": {"rationale": "원문의 디먹서 설명과 일치하며 수정안까지 ' + "가" * 300
    assert stop_kind(_resp(content, "length")) == "truncated"


def test_content_then_trailing_whitespace_run_is_degenerate():
    """내용을 쓰다가 공백 루프로 빠진 경우도 퇴화다 — 잘린 지점이 공백 안에 있다."""
    content = '{"faithfulness": {"rationale": "문장이다", "score": 5},' + " " * (DEGENERATE_TAIL_CHARS + 5)
    assert stop_kind(_resp(content, "length")) == "degenerate"


def test_short_whitespace_tail_before_cut_is_still_truncated():
    """들여쓰기 몇 칸에서 끊긴 것을 퇴화로 부르지 않는다 — 창 안에 내용이 있다."""
    content = '{"faithfulness": {"rationale": "' + "가" * 300 + '",\n    '
    assert stop_kind(_resp(content, "length")) == "truncated"


def test_other_finish_reasons_pass_through():
    assert stop_kind(_resp("{}", "stop")) == "stop"
    assert stop_kind({"choices": [{"message": {}}]}) is None
    assert stop_kind("not a response") is None


def test_count_length_stops_splits_and_refuses_to_guess():
    rows = [
        {"stop_kinds": ["truncated"], "finish_reasons": ["length"]},
        {"stop_kinds": ["degenerate", "degenerate"], "finish_reasons": ["length", "length"]},
        {"stop_kinds": ["stop"], "finish_reasons": ["stop"]},
        {"finish_reasons": ["length"]},  # 분류 전 행 — 원본 없이 절단이라고 확정하지 않는다
        {},
    ]
    assert count_length_stops(rows) == {"truncated": 1, "degenerate": 1, "length_unclassified": 1}
