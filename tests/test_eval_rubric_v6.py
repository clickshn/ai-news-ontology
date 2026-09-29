"""완결성 루브릭 v6 — judge 는 요소 판정만, 슬롯·점수는 코드가 (ADR-021, D-097 ~ D-099).

실제 API 를 부르지 않는다. 여기서 고정하는 계약:
  1. 요소 판정을 **라벨 대비 빠짐없이** 읽지 못하면 점수를 내지 않는다 (기본값으로 채우지 않는다)
  2. 슬롯 판정은 요소 판정의 함수다 — v5 의 "이름만 → 부분" 이탈은 **구조상 나올 수 없다**
  3. v6 만 `ElementJudgement` 로 부른다. v3 대조군의 출력 모델은 그대로다 (D-090)
  4. 코드가 점수를 낸 행에는 판정어를 읽는 과정 지표를 **None** 으로 둔다
  5. `prompt_sha256` 은 줄바꿈과 무관하다 (D-098). `--item` 은 없는 ID 에서 호출 전에 멈춘다 (D-099)
"""

from __future__ import annotations

import copy
import hashlib

import pytest

from eval.analysis import repeat_stats
from eval.completeness import (
    ElementParseError,
    compute_completeness,
    parse_element_verdicts,
    state_from_elements,
)
from eval.runner import (
    CODE_SCORED_RUBRICS,
    JUDGE_PROMPT_DIR,
    EvalError,
    evaluate_item,
    judge_summary,
    load_judge_prompt,
    main,
    prompt_sha256,
    select_items,
    summarize,
)
from eval.schema import CompletenessSlots, ElementJudgement, GoldenItem, ItemScore, SummaryJudgement
from extraction.schema import NewsOntology
from tests.test_eval_runner import GOLDEN_PAYLOAD, JUDGEMENT_PAYLOAD, ONTOLOGY_PAYLOAD, FakeJudge

#: 골든셋 #33001 의 실제 라벨과 같은 모양 (분모 4, 요소 1 / 1 / 3 / 2).
SLOTS_33001 = {
    "사건": ["Sony PS2 VPK 디먹서의 0 나누기 버그"],
    "범위": ["신뢰할 수 없는 입력(.vpk 파일·스트림)을 여는 FFmpeg 연동 애플리케이션"],
    "정도": ["서비스 거부(DoS) 성격", "Medium 등급", "메모리 손상·임의 읽기/쓰기 없음"],
    "경위": ["퍼저가 495,211회·10시간 43분 실행 끝에 발견", "채널 수 0 검증 누락이 원인"],
}

#: v5 본 실행(20260929T000543Z)에서 judge 가 #33001 에 10/10 쓴 근거 그대로.
V5_RATIONALE_33001 = (
    "①사건: 충족 — (a) 담김 | ②범위: 부분 — (a) 이름만 ('.vpk 파일을 여는 애플리케이션'은 "
    "언급되었으나 '신뢰할 수 없는 입력'이나 'FFmpeg 연동'이라는 구체적 범위가 누락됨) | "
    "③정도: 부분 — (a) 담김 / (b) 없음 / (c) 없음 | ④경위: 미충족 — (a) 없음 / (b) 없음 "
    "→ 합계 2 / 분모 4 → 점수 3"
)

V6_RATIONALE = (
    "①사건: (a) 담김 | ②범위: (a) 이름만 (FFmpeg 연동이 없다) | "
    "③정도: (a) 담김 / (b) 없음 / (c) 없음 | ④경위: (a) 없음 / (b) 없음"
)


def slots(payload: dict | None = None) -> CompletenessSlots:
    return CompletenessSlots.model_validate(payload or SLOTS_33001)


def golden(slot_payload: dict | None = SLOTS_33001) -> GoldenItem:
    payload = copy.deepcopy(GOLDEN_PAYLOAD)
    if slot_payload is not None:
        payload["completeness_slots"] = slot_payload
    return GoldenItem.model_validate(payload)


def v6_payload(rationale: str = V6_RATIONALE) -> dict:
    payload = copy.deepcopy(JUDGEMENT_PAYLOAD)
    payload["completeness"] = {"rationale": rationale}
    return payload


V6 = "summary_quality.v6.md"


# ---------------------------------------------------------------------------
# 1. 파서 — 라벨 대비 빠짐없이, 모순 없이
# ---------------------------------------------------------------------------
def test_parser_reads_v6_format():
    assert parse_element_verdicts(V6_RATIONALE, slots()) == {
        1: {"a": "담김"},
        2: {"a": "이름만"},
        3: {"a": "담김", "b": "없음", "c": "없음"},
        4: {"a": "없음", "b": "없음"},
    }


def test_parser_also_reads_v5_rationale():
    """v5 근거의 슬롯 판정어·합계는 무시하고 요소 판정만 읽는다 — 재계산에 쓴다."""
    assert parse_element_verdicts(V5_RATIONALE_33001, slots())[2] == {"a": "이름만"}


def test_parser_has_no_window_limit():
    """v4/v5 사후 관찰 파서의 200자 창은 긴 설명 뒤의 (b)(c) 를 잘라낸다. 여기는 창이 없다."""
    long = "③정도: (a) 담김 (" + "가" * 300 + ") / (b) 없음 / (c) 없음"
    rationale = V6_RATIONALE.replace("③정도: (a) 담김 / (b) 없음 / (c) 없음", long)
    assert parse_element_verdicts(rationale, slots())[3] == {"a": "담김", "b": "없음", "c": "없음"}


@pytest.mark.parametrize(
    "broken, reason",
    [
        (V6_RATIONALE.replace(" / (c) 없음", ""), "빠짐"),
        (V6_RATIONALE.replace("④경위: (a) 없음 / (b) 없음", "④경위: (a) 없음 / (b) 없음 / (c) 없음"), "남음"),
        (V6_RATIONALE.replace("| ②범위: (a) 이름만 (FFmpeg 연동이 없다) ", ""), "②범위 표기가 없다"),
        (V6_RATIONALE + " | ②범위: (a) 담김", "두 번"),
        (V6_RATIONALE.replace("①사건: (a) 담김", "①사건: (a) 담김 / (a) 없음"), "판정이 둘"),
        ("", "비어"),
    ],
)
def test_parser_refuses_instead_of_filling_defaults(broken, reason):
    with pytest.raises(ElementParseError, match=reason):
        parse_element_verdicts(broken, slots())


def test_excluded_slot_is_not_read():
    payload = {**SLOTS_33001, "경위": []}
    rationale = V6_RATIONALE.replace("④경위: (a) 없음 / (b) 없음", "④경위: 분모 제외")
    assert parse_element_verdicts(rationale, slots(payload))[4] == {}
    # 분모 제외 슬롯은 표기가 아예 없어도 된다
    assert parse_element_verdicts(rationale.split(" | ④")[0], slots(payload))[4] == {}


# ---------------------------------------------------------------------------
# 2. 계산 — 슬롯 판정은 요소 판정의 함수다
# ---------------------------------------------------------------------------
def test_name_only_is_never_partial():
    """v5 의 체계적 이탈(이름만 → 부분)이 구조상 나올 수 없다."""
    assert state_from_elements({"a": "이름만"}) == "none"
    assert state_from_elements({"a": "없음", "b": "이름만"}) == "none"


def test_v5_33001_recomputes_to_two():
    """v5 judge 는 3 을 적었고, 자기 요소 판정대로면 2 다 (run-log-session-13 §4)."""
    comp = compute_completeness(V5_RATIONALE_33001, slots())
    assert comp.states == {"①사건": "full", "②범위": "none", "③정도": "partial", "④경위": "none"}
    assert (comp.points, comp.denominator, comp.score) == (1.5, 4, 2)


def test_event_missing_scores_one():
    rationale = V6_RATIONALE.replace("①사건: (a) 담김", "①사건: (a) 이름만")
    assert compute_completeness(rationale, slots()).score == 1


def test_denominator_comes_from_label():
    payload = {**SLOTS_33001, "경위": []}
    rationale = "①사건: (a) 담김 | ②범위: (a) 담김 | ③정도: (a) 담김 / (b) 담김 / (c) 담김 | ④경위: 분모 제외"
    comp = compute_completeness(rationale, slots(payload))
    assert (comp.denominator, comp.score, comp.states["④경위"]) == (3, 5, "excluded")
    assert "④경위" not in comp.elements


# ---------------------------------------------------------------------------
# 3. runner — v6 만 코드 계산
# ---------------------------------------------------------------------------
def test_only_v6_is_code_scored():
    assert CODE_SCORED_RUBRICS == {V6}
    assert (JUDGE_PROMPT_DIR / V6).exists()


def test_v6_calls_element_model_and_records_computed_score():
    judge = FakeJudge(v6_payload())
    judgement, name, _, comp = judge_summary(golden(), "요약", judge, prompt=load_judge_prompt(V6))
    assert judge.calls[0]["output_model"] is ElementJudgement
    assert isinstance(judgement, SummaryJudgement)
    assert judgement.completeness.score == comp.score == 2
    assert judgement.completeness.rationale == V6_RATIONALE
    assert name == V6


@pytest.mark.parametrize("rubric", ["summary_quality.v3.md", "summary_quality.v5.md"])
def test_control_rubrics_keep_summary_judgement(rubric):
    """v3 대조군의 디코딩 제약이 바뀌면 대조군이 아니다 (D-090)."""
    judge = FakeJudge()
    *_, comp = judge_summary(golden(), "요약", judge, prompt=load_judge_prompt(rubric))
    assert judge.calls[0]["output_model"] is SummaryJudgement
    assert comp is None


def test_v6_without_labels_stops_before_call():
    judge = FakeJudge(v6_payload())
    with pytest.raises(EvalError):
        judge_summary(golden(None), "요약", judge, prompt=load_judge_prompt(V6))
    assert judge.calls == []


def test_item_score_marks_who_scored():
    actual = NewsOntology.model_validate(ONTOLOGY_PAYLOAD)
    coded = evaluate_item(golden(), actual, judge_client=FakeJudge(v6_payload()), judge_prompt=load_judge_prompt(V6))
    judged = evaluate_item(golden(), actual, judge_client=FakeJudge(), judge_prompt=load_judge_prompt("summary_quality.v3.md"))
    skipped = evaluate_item(golden(), actual)
    assert coded.completeness_scored_by == "code"
    assert coded.completeness_computation is not None and coded.completeness_computation.score == 2
    assert judged.completeness_scored_by == "judge" and judged.completeness_computation is None
    assert skipped.completeness_scored_by is None


def test_parse_failure_is_a_judge_failure_not_a_low_score():
    actual = NewsOntology.model_validate(ONTOLOGY_PAYLOAD)
    broken = v6_payload(V6_RATIONALE.replace(" / (c) 없음", ""))
    score = evaluate_item(golden(), actual, judge_client=FakeJudge(broken), judge_prompt=load_judge_prompt(V6))
    assert score.judgement is None
    assert score.completeness_scored_by is None
    assert score.errors[0].startswith("judge 실패: 요소 판정 파싱")
    assert score.metadata.judge_prompt == V6
    stats = repeat_stats([score])
    assert (stats.failures, stats.element_parse_failures, stats.n) == (1, 1, 0)


def test_old_records_without_the_field_still_load():
    row = evaluate_item(golden(), NewsOntology.model_validate(ONTOLOGY_PAYLOAD), judge_client=FakeJudge()).model_dump(mode="json")
    row.pop("completeness_scored_by")
    row.pop("completeness_computation")
    assert ItemScore.model_validate(row).completeness_scored_by is None


# ---------------------------------------------------------------------------
# 4. 집계 — 코드 행에 판정어 지표를 돌리지 않는다
# ---------------------------------------------------------------------------
def _coded_rows(n: int = 3) -> list[ItemScore]:
    actual = NewsOntology.model_validate(ONTOLOGY_PAYLOAD)
    prompt = load_judge_prompt(V6)
    return [
        evaluate_item(golden(), actual, judge_client=FakeJudge(v6_payload()), judge_prompt=prompt, repeat_index=i)
        for i in range(n)
    ]


def test_process_metrics_are_none_for_code_rows():
    stats = repeat_stats(_coded_rows())
    assert stats.completeness_scored_by == "code"
    assert stats.state_adherence is None
    assert stats.score_table_adherence is None
    assert stats.element_slot_consistency is None
    assert stats.denominator_adherence is None
    assert stats.slot_adherence is None
    assert stats.completeness.distribution == {"2": 3}


def test_code_rows_count_states_and_elements_from_computation():
    stats = repeat_stats(_coded_rows())
    assert stats.slot_state_counts["2범위"] == {"none": 3}
    assert stats.element_verdict_counts["2범위(a)"] == {"이름만": 3}
    assert stats.element_verdict_counts["3정도(c)"] == {"없음": 3}


def test_summary_warns_scores_are_not_comparable():
    notes = summarize(_coded_rows(1), run_id="t").notes
    assert any("코드가 계산한 값" in n for n in notes)


# ---------------------------------------------------------------------------
# 5. 루브릭 파일
# ---------------------------------------------------------------------------
def test_v6_shows_no_score_table_or_tally():
    v6 = load_judge_prompt(V6).system
    assert "0.75 ≤ r" not in v6
    assert "→ 합계" not in v6
    assert "충족 —" not in v6 and "미충족" not in v6


def test_v6_has_no_multiline_output_instruction():
    v6 = load_judge_prompt(V6).system
    assert "```" not in v6
    assert "한 줄" in v6


def test_v6_keeps_v5_text_where_it_should():
    v5 = load_judge_prompt("summary_quality.v5.md").system
    v6 = load_judge_prompt(V6).system

    def section(text: str, start: str, end: str) -> str:
        return text[text.index(start) : text.index(end)]

    assert section(v5, "### 1. 충실성", "### 2. 완결성") == section(v6, "### 1. 충실성", "### 2. 완결성")
    assert section(v5, "**요소 판정**", "**슬롯 판정**") == section(v6, "**요소 판정**", "**너는 요소 판정만")
    assert section(v5, "### 3. 간결성", "## 예시") == section(v6, "### 3. 간결성", "## 예시")
    assert v5[v5.index("## 충실성 인용") :] == v6[v6.index("## 충실성 인용") :]
    assert load_judge_prompt("summary_quality.v5.md").user == load_judge_prompt(V6).user


def test_v6_example_is_parseable():
    """루브릭의 예시 근거가 파서를 통과해야 한다 — 예시가 틀린 형식을 가르치면 안 된다."""
    v6 = load_judge_prompt(V6).system
    example = v6.split("근거: `")[1].split("`")[0]
    example_slots = CompletenessSlots.model_validate(
        {"사건": ["x"], "범위": ["y"], "정도": ["z", "w"], "경위": []}
    )
    assert compute_completeness(example, example_slots).score == 3


# ---------------------------------------------------------------------------
# 6. prompt_sha256 — 줄바꿈과 무관 (D-098)
# ---------------------------------------------------------------------------
def test_hash_ignores_line_endings(tmp_path):
    (tmp_path / "lf.md").write_bytes(b"# System\nA\n\n# User\nB\n")
    (tmp_path / "crlf.md").write_bytes(b"# System\r\nA\r\n\r\n# User\r\nB\r\n")
    assert prompt_sha256("lf.md", prompt_dir=tmp_path) == prompt_sha256("crlf.md", prompt_dir=tmp_path)


def test_hash_equals_raw_hash_of_lf_file(tmp_path):
    """LF 로 체크아웃된 파일에서는 이전(원시 바이트) 값과 같다 — v4·v5 기록이 끊기지 않는다."""
    data = b"# System\nA\n# User\nB\n"
    (tmp_path / "p.md").write_bytes(data)
    assert prompt_sha256("p.md", prompt_dir=tmp_path) == hashlib.sha256(data).hexdigest()[:16]


@pytest.mark.parametrize(
    "name, expected",
    [
        ("summary_quality.v3.md", "670f4eccdad2574a"),  # CRLF 작업본 원시값 70d7ed2fa0d235b2
        ("summary_quality.v4.md", "723f9b3e603d0314"),
        ("summary_quality.v5.md", "d29cda1ba37e9f37"),
    ],
)
def test_recorded_rubric_hashes(name, expected):
    """`eval/judge_prompts/README.md` 대응표의 값. 체크아웃 줄바꿈과 무관하게 같아야 한다."""
    assert prompt_sha256(name) == expected


# ---------------------------------------------------------------------------
# 7. --item (D-099)
# ---------------------------------------------------------------------------
def test_select_items_keeps_order_and_refuses_unknown():
    a = golden()
    b = GoldenItem.model_validate({**copy.deepcopy(GOLDEN_PAYLOAD), "id": "b"})
    assert [i.id for i in select_items([a, b], ["b"])] == ["b"]
    assert [i.id for i in select_items([a, b], ["b", a.id])] == [a.id, "b"]
    with pytest.raises(EvalError, match="없는 항목: nope"):
        select_items([a, b], ["b", "nope"])


def test_unknown_item_stops_before_any_judge_client(tmp_path, monkeypatch, capsys):
    import json

    import eval.runner as runner

    gdir = tmp_path / "golden"
    gdir.mkdir()
    (gdir / "a.json").write_text(json.dumps(GOLDEN_PAYLOAD, ensure_ascii=False), encoding="utf-8")
    preds = tmp_path / "preds.jsonl"
    preds.write_text(json.dumps({"item_id": GOLDEN_PAYLOAD["id"], "ontology": ONTOLOGY_PAYLOAD}, ensure_ascii=False), encoding="utf-8")

    def boom(*_a, **_k):
        raise AssertionError("judge 클라이언트를 만들면 안 된다")

    monkeypatch.setattr(runner, "judge_client_from_config", boom)
    code = main(["--golden-set", str(gdir), "--predictions", str(preds), "--scores-dir", str(tmp_path / "s"), "--judge", "--item", "typo"])
    assert code == 1
    assert "없는 항목: typo" in capsys.readouterr().err
    assert not (tmp_path / "s").exists()
