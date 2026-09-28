"""완결성 루브릭 v4 — 분모를 라벨로 고정하고 부분 충족을 둔다 (D-089 ~ D-091).

실제 API 를 부르지 않는다. 여기서 고정하는 계약:
  1. 슬롯 라벨(`completeness_slots`)의 모양 — 빈 목록 = 분모 제외, 네 키 필수
  2. v4 루브릭에 라벨이 **없으면 호출 전에 멈춘다** — '(정보 없음)' 으로 채워진 채
     나가면 judge 가 스스로 분모를 정하고, 점수는 멀쩡히 나와 구분이 안 된다
  3. 집계기가 4상태 판정·분모·점수표 준수를 **점수와 독립적으로** 읽는다
  4. v3 는 한 글자도 바뀌지 않았다 (session-12 기준선의 해시 그대로)
"""

from __future__ import annotations

import copy

import pytest
from pydantic import ValidationError

from eval.analysis import (
    completeness_from_states,
    repeat_stats,
    slot_states,
    slot_verdicts,
    stated_denominator,
)
from eval.runner import (
    EvalError,
    build_judge_variables,
    check_judge_inputs,
    evaluate_item,
    judge_summary,
    load_judge_prompt,
    prompt_requires_completeness_slots,
    prompt_sha256,
    render_completeness_slots,
)
from eval.schema import CompletenessSlots, GoldenItem
from extraction.schema import NewsOntology
from tests.test_eval_runner import GOLDEN_PAYLOAD, JUDGEMENT_PAYLOAD, ONTOLOGY_PAYLOAD, FakeJudge

SLOTS = {
    "사건": ["0 나누기 버그"],
    "범위": ["FFmpeg 연동 애플리케이션"],
    "정도": ["DoS 성격", "Medium 등급"],
    "경위": [],
}


def golden_with(slots: dict | None) -> GoldenItem:
    payload = copy.deepcopy(GOLDEN_PAYLOAD)
    if slots is not None:
        payload["completeness_slots"] = slots
    return GoldenItem.model_validate(payload)


def judged(rationale: str, score: int) -> dict:
    payload = copy.deepcopy(JUDGEMENT_PAYLOAD)
    payload["completeness"] = {"rationale": rationale, "score": score}
    return payload


V4_RATIONALE = (
    "①사건: 충족 — (a) 담김\n"
    "②범위: 미충족 — (a) 없음\n"
    "③정도: 부분 — (a) 담김 / (b) 없음\n"
    "④경위: 분모 제외\n"
    "→ 합계 1.5 / 분모 3 → 점수 3"
)

# session-12 에 저장된 실제 v3 근거 (S3Gym). 0회차만 ③ 을 분모에서 뺐다 (D-088).
V3_S3GYM_EXCLUDED = (
    "원문이 제공하는 슬롯은 ①사건, ②범위, ④경위이다(③정도에 해당하는 구체적 수치는 "
    "제시되지 않음). 분모는 3개 슬롯이다. 1) ①사건: S3Gym 벤치마크 제안 (충족), 2) ②범위: "
    "LLM의 자기 개선 능력 및 7가지 텍스트 기반 게임 (충족), 3) ④경위: 자기 테스트/판단/개선 "
    "능력 측정 및 세 가지 경험 반영 경로 평가 (충족). 3/3 슬롯을 모두 채웠다."
)
V3_S3GYM_FULL = (
    "원문이 제공하는 정보 기준 분모는 4슬롯이다. ① 사건: S3Gym 벤치마크 제안 (충족), ② 범위: "
    "LLM 에이전트 (충족), ③ 정도: 성능 향상 정도와 효과가 다르게 나타남 (충족 - 구체적 수치는 "
    "없으나 결과의 경향성을 언급함), ④ 경위: 미충족 (어떤 방식으로 벤치마크가 설계되었는지, "
    "혹은 어떤 실험 과정을 통해 결과가 나왔는지에 대한 구체적 방법론이 빠져 있음). → 3슬롯 충족"
)


# ---------------------------------------------------------------------------
# 1. 라벨 스키마
# ---------------------------------------------------------------------------
def test_empty_slot_is_excluded_from_denominator():
    assert CompletenessSlots.model_validate(SLOTS).denominator == 3


def test_all_four_keys_are_required():
    """키가 빠진 것과 '원문이 주지 않았다고 판단했다'(빈 목록)를 구분한다."""
    partial = {k: v for k, v in SLOTS.items() if k != "경위"}
    with pytest.raises(ValidationError):
        CompletenessSlots.model_validate(partial)


def test_event_slot_cannot_be_empty():
    with pytest.raises(ValidationError):
        CompletenessSlots.model_validate({**SLOTS, "사건": []})


def test_element_cannot_sit_in_two_slots():
    with pytest.raises(ValidationError, match="두 슬롯"):
        CompletenessSlots.model_validate({**SLOTS, "경위": ["DoS 성격"]})


def test_blank_element_rejected():
    with pytest.raises(ValidationError, match="빈 요소"):
        CompletenessSlots.model_validate({**SLOTS, "범위": ["  "]})


def test_at_most_three_elements_per_slot():
    with pytest.raises(ValidationError):
        CompletenessSlots.model_validate({**SLOTS, "정도": ["a", "b", "c", "d"]})


def test_golden_item_without_slots_still_loads():
    """v3 경로는 계속 유효하므로 confirmed 필수가 아니다."""
    assert golden_with(None).completeness_slots is None


# ---------------------------------------------------------------------------
# 2. judge 입력
# ---------------------------------------------------------------------------
def test_render_shows_denominator_and_exclusion():
    text = render_completeness_slots(CompletenessSlots.model_validate(SLOTS))
    assert text.splitlines()[0] == "분모: 3"
    assert "③정도: (a) DoS 성격 / (b) Medium 등급" in text
    assert "④경위: 분모 제외" in text


def test_v4_requires_slots_and_v3_does_not():
    assert prompt_requires_completeness_slots(load_judge_prompt("summary_quality.v4.md"))
    assert not prompt_requires_completeness_slots(load_judge_prompt("summary_quality.v3.md"))


def test_v4_stops_before_call_when_labels_missing():
    judge = FakeJudge()
    with pytest.raises(EvalError, match="completeness_slots"):
        judge_summary(golden_with(None), "요약", judge, prompt=load_judge_prompt("summary_quality.v4.md"))
    assert judge.calls == [], "라벨 없이 호출이 나갔다"


def test_preflight_names_every_unlabeled_item():
    items = [golden_with(SLOTS), golden_with(None)]
    with pytest.raises(EvalError, match=GOLDEN_PAYLOAD["id"]):
        check_judge_inputs(items, load_judge_prompt("summary_quality.v4.md"))


def test_v3_runs_without_labels():
    check_judge_inputs([golden_with(None)], load_judge_prompt("summary_quality.v3.md"))


def test_v4_prompt_receives_rendered_slots():
    judge = FakeJudge(judged(V4_RATIONALE, 3))
    judge_summary(golden_with(SLOTS), "요약", judge, prompt=load_judge_prompt("summary_quality.v4.md"))
    user = judge.calls[0]["user"]
    assert "분모: 3" in user
    assert "(정보 없음)" not in user


def test_v3_prompt_is_unaffected_by_labels():
    """라벨이 있어도 v3 가 받는 텍스트는 session-12 와 같아야 대조군이 된다."""
    v3 = load_judge_prompt("summary_quality.v3.md")
    with_labels = v3.render(**build_judge_variables(golden_with(SLOTS), "요약"))
    without = v3.render(**build_judge_variables(golden_with(None), "요약"))
    assert with_labels == without


def test_item_score_records_labeled_denominator(tmp_path):
    ontology = NewsOntology.model_validate(ONTOLOGY_PAYLOAD)
    assert evaluate_item(golden_with(SLOTS), ontology).labeled_denominator == 3
    assert evaluate_item(golden_with(None), ontology).labeled_denominator is None


# ---------------------------------------------------------------------------
# 3. 집계 — 4상태·분모·점수표
# ---------------------------------------------------------------------------
def test_slot_states_reads_v4_format():
    assert slot_states(V4_RATIONALE) == {1: "full", 2: "none", 3: "partial", 4: "excluded"}


def test_first_verdict_word_wins():
    """판정어 뒤의 요소 설명에 '미충족'·'없음' 이 또 나와도 슬롯 판정은 앞의 것이다."""
    states = slot_states("①사건: 충족 — (a) 담김 ②범위: 부분 — (a) 담김 / (b) 미충족 수준 "
                         "③정도: 충족 ④경위: 충족")
    assert states[2] == "partial"


def test_partial_is_not_read_as_full():
    """`slot_verdicts` 는 '부분 충족' 을 충족으로 읽는다 — 그래서 4상태 파서를 따로 뒀다."""
    text = "①사건: 충족 ②범위: 부분 충족 ③정도: 충족 ④경위: 충족"
    assert slot_verdicts(text)[2] is True
    assert slot_states(text)[2] == "partial"


def test_stated_denominator_v4_and_v3_forms():
    assert stated_denominator(V4_RATIONALE) == 3
    assert stated_denominator(V3_S3GYM_EXCLUDED) == 3
    assert stated_denominator(V3_S3GYM_FULL) == 4
    assert stated_denominator("①충족 ②충족 ③충족 ④미충족 → 3/4 슬롯 충족") == 4
    assert stated_denominator("근거 없음") is None


def test_exclusion_word_is_not_read_as_denominator():
    assert stated_denominator("④경위: 분모 제외\n→ 합계 2 / 분모 3 → 점수 4") == 3


@pytest.mark.parametrize(
    ("states", "expected"),
    [
        ({1: "full", 2: "full", 3: "full", 4: "full"}, 5),
        ({1: "full", 2: "full", 3: "full", 4: "none"}, 4),       # v3 3/4 = 4 와 같다
        ({1: "full", 2: "full", 3: "partial", 4: "none"}, 3),    # 2.5/4 = 0.625
        ({1: "full", 2: "full", 3: "full", 4: "partial"}, 4),    # 3.5/4
        ({1: "full", 2: "none", 3: "none", 4: "none"}, 2),       # v3 1/4 = 2 와 같다
        ({1: "full", 2: "full", 3: "none", 4: "excluded"}, 3),   # 2/3 — v3 의 '≈3~4' 를 3 으로
        ({1: "full", 2: "full", 3: "partial", 4: "excluded"}, 4),  # 2.5/3
        ({1: "none", 2: "full", 3: "full", 4: "full"}, 1),       # ① 미충족이면 1
        ({1: "full", 2: "full", 3: None, 4: "full"}, None),      # 판정이 비면 계산하지 않는다
    ],
)
def test_score_table(states, expected):
    assert completeness_from_states(states) == expected


def _scores(rationales_and_scores, slots=SLOTS):
    ontology = NewsOntology.model_validate(ONTOLOGY_PAYLOAD)
    golden = golden_with(slots)
    return [
        evaluate_item(golden, ontology, judge_client=FakeJudge(judged(r, s)), repeat_index=i)
        for i, (r, s) in enumerate(rationales_and_scores)
    ]


def test_repeat_stats_v4_adherence():
    stats = repeat_stats(_scores([(V4_RATIONALE, 3), (V4_RATIONALE, 4)]))
    assert stats.labeled_denominator == 3
    assert stats.denominator_adherence == 1.0
    assert stats.state_adherence == 1.0
    assert stats.score_table_adherence == 0.5, "4점 회차는 표를 적용하지 않았다"
    assert stats.slot_state_counts["3정도"] == {"partial": 2}


def test_session12_denominator_drift_is_visible():
    """session-12 의 S3Gym 은 slot_adherence 1.0 인데 분모가 흔들렸다 — 이제 드러난다."""
    s3gym = {"사건": ["S3Gym 제안"], "범위": ["7개 게임"], "정도": ["경로별 차이"], "경위": ["held-out 분리"]}
    rows = [(V3_S3GYM_EXCLUDED, 5)] + [(V3_S3GYM_FULL, 4)] * 4
    stats = repeat_stats(_scores(rows, slots=s3gym))
    assert stats.labeled_denominator == 4
    assert stats.denominator_adherence == 0.8


def test_adherence_none_without_labels():
    stats = repeat_stats(_scores([(V4_RATIONALE, 3)], slots=None))
    assert stats.labeled_denominator is None
    assert stats.denominator_adherence is None


# ---------------------------------------------------------------------------
# 4. 루브릭 파일
# ---------------------------------------------------------------------------
def test_v3_is_unchanged_since_session12_baseline():
    """바뀌면 v3 대조군이 대조군이 아니다.

    ⚠️ session-12 기록의 `judge_prompt_sha256 = 70d7ed2fa0d235b2` 는 **CRLF 로 체크아웃된
    작업본**의 해시다. `prompt_sha256` 이 원시 바이트를 해시하므로 LF 체크아웃에서는 같은
    파일이 다른 값을 낸다(모델이 받는 텍스트는 같다 — `read_text` 가 줄바꿈을 정규화한다).
    그래서 여기서는 줄바꿈을 LF 로 맞춘 내용 해시로 고정한다. 커밋된 blob 의 값이다.
    """
    import hashlib

    from eval.runner import JUDGE_PROMPT_DIR

    raw = (JUDGE_PROMPT_DIR / "summary_quality.v3.md").read_bytes().replace(b"\r\n", b"\n")
    assert hashlib.sha256(raw).hexdigest()[:16] == "670f4eccdad2574a"


def test_v4_keeps_faithfulness_and_concision_text_of_v3():
    v3 = load_judge_prompt("summary_quality.v3.md").system
    v4 = load_judge_prompt("summary_quality.v4.md").system

    def section(text: str, start: str, end: str) -> str:
        return text[text.index(start) : text.index(end)]

    last = "- 1: 요약이라 부르기 어렵다."
    assert section(v3, "### 1. 충실성", "### 2. 완결성") == section(v4, "### 1. 충실성", "### 2. 완결성")
    assert section(v3, "### 3. 간결성", last) == section(v4, "### 3. 간결성", last)


def test_v4_tells_judge_not_to_decide_denominator():
    v4 = load_judge_prompt("summary_quality.v4.md").system
    assert "분모에서 빼지 않는다" in v4
    assert "부분" in v4 and "0.5" in v4


def test_v4_drops_the_real_boundary_case():
    """채점 대상 항목의 판정이 루브릭에 있으면 그 항목은 정답지를 보고 채점된다."""
    v4 = load_judge_prompt("summary_quality.v4.md")
    assert "495,211회·10시간 43분" not in v4.system
    assert "가상" in v4.system


def test_v4_fixes_the_ai_news_wording():
    """D-055 가 v4 로 미뤄 둔 정정."""
    v4 = load_judge_prompt("summary_quality.v4.md").system
    assert "AI 관련 뉴스·논문" in v4
    assert "AI 뉴스 요약의" not in v4


# ---------------------------------------------------------------------------
# 5. v5 — 한 줄 형식 (D-094). v4 와 형식 지시 외에는 같아야 원인을 가를 수 있다
# ---------------------------------------------------------------------------
V5_RATIONALE = (
    "①사건: 충족 — (a) 담김 | ②범위: 미충족 — (a) 없음 | ③정도: 부분 — (a) 담김 / (b) 없음 "
    "| ④경위: 분모 제외 → 합계 1.5 / 분모 3 → 점수 3"
)


def test_parser_reads_v5_single_line_format():
    assert slot_states(V5_RATIONALE) == {1: "full", 2: "none", 3: "partial", 4: "excluded"}
    assert stated_denominator(V5_RATIONALE) == 3
    assert completeness_from_states(slot_states(V5_RATIONALE)) == 3


def test_v5_has_no_multiline_output_instruction():
    """퇴화(D-093)의 유력한 원인이던 여러 줄 형식 지시·코드블록이 없다."""
    v5 = load_judge_prompt("summary_quality.v5.md").system
    assert "```" not in v5
    assert "줄을 바꾸지 않는다" in v5


def test_v5_differs_from_v4_only_in_output_format():
    import re

    def strip_format(text: str) -> str:
        text = re.sub(r"\*\*근거는 반드시.*?(?=\n> 완결성과)", "", text, flags=re.S)
        return re.sub(r"\*\*요약\*\*:.*?(?=r = 0\.5)", "", text, flags=re.S)

    v4 = load_judge_prompt("summary_quality.v4.md")
    v5 = load_judge_prompt("summary_quality.v5.md")
    assert v4.user == v5.user
    assert strip_format(v4.system) == strip_format(v5.system)


def test_v5_requires_slots():
    assert prompt_requires_completeness_slots(load_judge_prompt("summary_quality.v5.md"))
