"""반복 채점 집계(`eval/analysis.py`)와 runner 의 반복 실행 계약.

judge 를 실제로 부르지 않는다 — 집계는 순수 함수이고, 반복 루프는 가짜
클라이언트로 확인한다. 채점 로직을 고치는 동안 API 를 부르면 CLAUDE.md 의
호출 규칙이 무의미해진다.
"""

from __future__ import annotations

import json

import pytest

from eval.analysis import axis_stats, repeat_stats, slot_verdicts
from eval.runner import evaluate_item, summarize
from eval.schema import (
    Criterion,
    HumanSummaryScores,
    ItemScore,
    RunMetadata,
    SummaryJudgement,
)

# ---------------------------------------------------------------------------
# 슬롯 판정 읽기
# ---------------------------------------------------------------------------
def test_slot_verdicts_reads_v3_style_rationale():
    rationale = "①사건 충족, ②범위 미충족, ③정도 미충족, ④경위는 이름만 언급이라 미충족 → 1슬롯"
    assert slot_verdicts(rationale) == {1: True, 2: False, 3: False, 4: False}


def test_slot_verdicts_prefers_negative_over_substring():
    """'미충족' 은 '충족' 을 부분 문자열로 포함한다 — 순진하게 찾으면 전부 충족이 된다."""
    assert slot_verdicts("①미충족 ②미충족 ③미충족 ④미충족")[1] is False


def test_slot_verdicts_marks_unmentioned_slots_none():
    """언급되지 않은 슬롯은 '미충족' 이 아니라 **판정 없음**이다.

    둘을 같은 값으로 접으면 '루브릭을 따랐는데 다 미충족' 과 '루브릭을 아예
    안 따랐다' 가 구분되지 않는다. 후속 조치가 완전히 다르다.
    """
    verdicts = slot_verdicts("①사건은 충족했다. 나머지는 부족하다.")
    assert verdicts[1] is True
    assert verdicts[2] is None and verdicts[3] is None and verdicts[4] is None


def test_slot_verdicts_accepts_plain_number_notation():
    verdicts = slot_verdicts("슬롯 1 충족, 슬롯 2 미충족, 슬롯 3 미충족, 슬롯 4 미충족")
    assert verdicts == {1: True, 2: False, 3: False, 4: False}


def test_slot_verdicts_empty_rationale():
    assert slot_verdicts("") == {1: None, 2: None, 3: None, 4: None}


def test_slot_verdicts_ignores_v2_style_rationale():
    """v2 근거에는 슬롯 표기가 없다 — 없는 것을 있다고 읽으면 안 된다."""
    v2 = "무슨 일이 있었는지는 담았으나 21바이트 재현·영향 범위·Medium 등급이 빠졌다."
    assert all(v is None for v in slot_verdicts(v2).values())


# ---------------------------------------------------------------------------
# 축 통계
# ---------------------------------------------------------------------------
def test_axis_stats_distribution_and_mode():
    stats = axis_stats("completeness", [2, 2, 3, 2, 3])
    assert stats.n == 5
    assert stats.mode == 2
    assert stats.distribution == {"2": 3, "3": 2}
    assert stats.spread == 1
    assert stats.mode_ratio == pytest.approx(0.6)


def test_axis_stats_single_sample_has_no_stdev():
    """1회 관측의 편차를 0 으로 적으면 '흔들리지 않았다'로 읽힌다 — 재지 못한 것이다."""
    assert axis_stats("completeness", [3]).stdev is None


def test_axis_stats_breaks_mode_tie_downward():
    """동점이면 낮은 쪽. 채점을 관대한 쪽으로 반올림하지 않는다."""
    assert axis_stats("completeness", [2, 3]).mode == 2


def test_axis_stats_empty_is_none():
    assert axis_stats("completeness", []) is None


# ---------------------------------------------------------------------------
# 반복 집계
# ---------------------------------------------------------------------------
def _score(item_id, completeness, *, rationale="①충족 ②미충족 ③미충족 ④미충족", prompt="v3", judged=True):
    judgement = None
    if judged:
        judgement = SummaryJudgement(
            faithfulness=Criterion(rationale="원문으로 검증된다", score=4),
            completeness=Criterion(rationale=rationale, score=completeness),
            concision=Criterion(rationale="군더더기가 없다", score=5),
        )
    return ItemScore(
        item_id=item_id,
        source_url="https://example.com",
        field_scores=[],
        judgement=judgement,
        human_summary_scores=HumanSummaryScores.model_validate(
            {"충실성": 5, "완결성": 1, "간결성": 5}
        ),
        metadata=RunMetadata(judge_prompt=prompt),
    )


def test_repeat_stats_aggregates_one_item():
    rows = [_score("a", c) for c in (2, 2, 3)]
    stats = repeat_stats(rows)
    assert stats.n == 3
    assert stats.completeness.mode == 2
    assert stats.judge_prompt == "v3"
    assert stats.slot_adherence == 1.0
    assert stats.slot_fill_rate["1사건"] == 1.0
    assert stats.slot_fill_rate["4경위"] == 0.0


def test_repeat_stats_gap_uses_human_score_per_axis():
    rows = [_score("a", c) for c in (2, 2, 2)]
    stats = repeat_stats(rows)
    assert stats.gap("completeness") == pytest.approx(1.0)  # judge 2.0 - 사람 1
    assert stats.gap("concision") == pytest.approx(0.0)


def test_repeat_stats_failures_do_not_enter_distribution():
    """실패를 0점으로 세면 분포가 아래로 끌려간다 — '낮게 채점' 이 아니라 '재지 못함' 이다."""
    failed = _score("a", 0, judged=False)
    failed.errors.append("judge 실패: APIError: 500")
    rows = [_score("a", 2), _score("a", 2), failed]
    stats = repeat_stats(rows)
    assert stats.n == 2
    assert stats.failures == 1
    assert stats.completeness.distribution == {"2": 2}


def test_repeat_stats_v2_rationale_reports_zero_adherence():
    rows = [_score("a", 3, rationale="핵심은 담았으나 세부가 빠졌다", prompt="v2")]
    stats = repeat_stats(rows)
    assert stats.slot_adherence == 0.0


def test_repeat_stats_mixed_prompts_leave_prompt_none():
    """두 루브릭 결과를 한 통계에 섞으면 어느 쪽 수치인지 말할 수 없다."""
    rows = [_score("a", 2, prompt="v3"), _score("a", 3, prompt="v2")]
    assert repeat_stats(rows).judge_prompt is None


def test_repeat_stats_ignores_other_items():
    rows = [_score("a", 2), _score("b", 5)]
    assert repeat_stats(rows, item_id="a").n == 1


# ---------------------------------------------------------------------------
# summarize 의 반복 계약
# ---------------------------------------------------------------------------
def test_summarize_counts_items_not_repeats():
    """10회 반복을 10건으로 세면 표본이 늘어난 것처럼 보인다 — D-043 이 경계한 착시."""
    rows = [_score("a", 2) for _ in range(5)]
    summary = summarize(rows, run_id="r")
    assert summary.item_count == 1
    assert summary.repeat == 5
    assert summary.judge_call_count == 5
    assert any("표본을 늘리는 것이 아니다" in n for n in summary.notes)


def test_summarize_attaches_repeat_stats():
    rows = [_score("a", c) for c in (2, 3, 2)]
    summary = summarize(rows, run_id="r")
    assert len(summary.repeat_stats) == 1
    assert summary.repeat_stats[0].completeness.distribution == {"2": 2, "3": 1}


def test_summarize_without_repeat_is_unchanged():
    summary = summarize([_score("a", 2)], run_id="r")
    assert summary.repeat == 1
    assert not any("반복 채점" in n for n in summary.notes)


def test_summary_round_trips_through_json():
    """`write_scores` 가 쓰는 것과 같은 직렬화. 통계가 파일에 남아야 재분석할 수 있다."""
    summary = summarize([_score("a", c) for c in (2, 3)], run_id="r")
    revived = json.loads(json.dumps(summary.model_dump(mode="json"), ensure_ascii=False))
    assert revived["repeat_stats"][0]["completeness"]["stdev"] == pytest.approx(0.707, abs=1e-3)


# ---------------------------------------------------------------------------
# 반복 루프 (가짜 클라이언트)
# ---------------------------------------------------------------------------
@pytest.fixture
def golden():
    from tests.test_eval_runner import GOLDEN_PAYLOAD
    from eval.schema import GoldenItem

    return GoldenItem.model_validate(GOLDEN_PAYLOAD)


@pytest.fixture
def ontology():
    from tests.test_eval_runner import ONTOLOGY_PAYLOAD
    from extraction.schema import NewsOntology

    return NewsOntology.model_validate(ONTOLOGY_PAYLOAD)


def test_evaluate_item_records_repeat_index(golden, ontology):
    score = evaluate_item(golden, ontology, repeat_index=3)
    assert score.repeat_index == 3


def test_evaluate_item_defaults_repeat_index_zero(golden, ontology):
    assert evaluate_item(golden, ontology).repeat_index == 0


def test_repeat_stats_does_not_count_unrun_judge_as_failure():
    """`--no-judge` 회차를 실패로 세면 'judge 가 N회 실패했다'는 거짓 기록이 남는다."""
    row = _score("a", 0, judged=False)  # errors 가 비어 있다 = 부르지 않았다
    assert repeat_stats([row]).failures == 0


def test_repeat_stats_counts_actual_judge_failure():
    row = _score("a", 0, judged=False)
    row.errors.append("judge 실패: RateLimitError: 429")
    assert repeat_stats([row]).failures == 1


# ---------------------------------------------------------------------------
# 실제 judge 근거로 만든 회귀 테스트 (2026-09-01 v3 실행에서 채집)
# ---------------------------------------------------------------------------
REAL_V3_RATIONALE = (
    "①사건(FFmpeg Sony PS2 VPK 디먹서의 0 나누기 버그) 충족, "
    "②범위(avformat_open_input·av_read_frame을 신뢰할 수 없는 데이터에 호출하는 "
    "모든 FFmpeg 연동 애플리케이션) 미충족, "
    "③정도(SIGFPE, Medium 등급 DoS, 메모리 손상 없음, 21바이트 재현) 미충족, "
    "④경위(495,211회 실행·10시간 43분, 채널 수 검증 우회)는 '퍼저가 찾아낸'이라는 "
    "이름만 언급으로 미충족 → 원문이 4슬롯을 모두 제공했는데 1슬롯만 충족."
)


def test_slot_verdicts_survives_long_parenthetical():
    """모델은 괄호에 근거를 길게 달고 **판정어를 맨 뒤에** 둔다.

    고정 글자 수로 창을 자르면 판정어가 밖으로 밀려 '언급 없음' 이 된다.
    첫 측정에서 실제로 이 때문에 준수율이 1.0 대신 0.5 로 나왔다.
    """
    assert slot_verdicts(REAL_V3_RATIONALE) == {1: True, 2: False, 3: False, 4: False}


def test_slot_verdicts_does_not_read_tally_as_verdict():
    """마지막 슬롯 뒤의 '→ 1슬롯만 충족' 은 총계다. 슬롯 판정으로 읽으면 뒤집힌다."""
    assert slot_verdicts(REAL_V3_RATIONALE)[4] is False


def test_slot_verdicts_handles_leading_slot_inventory():
    """원문이 제공한 슬롯을 먼저 나열한 뒤 요약을 판정하는 형식도 나온다."""
    rationale = (
        "원문은 4슬롯을 모두 제공한다(①VPK 디먹서 0 나누기 ②연동 애플리케이션 "
        "③Medium 등급 DoS ④퍼저 495,211회 실행). 요약은 ①충족, ②미충족, ③미충족, "
        "④는 '퍼저가 찾아냈다'는 이름만 언급이라 미충족 → 1/4슬롯."
    )
    # 첫 열거는 판정어가 없어 None 이고, 뒤의 판정이 채운다.
    assert slot_verdicts(rationale) == {1: True, 2: False, 3: False, 4: False}
