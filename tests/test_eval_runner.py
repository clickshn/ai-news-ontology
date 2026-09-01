"""eval 채점기 단위 테스트 (실제 API 호출 없음).

judge 는 `LLMClient` Protocol 을 만족하는 가짜 클라이언트로 대체한다. 이 파일이
API 를 부르면 테스트를 돌릴 때마다 비용이 나가고, CLAUDE.md 의 "실호출 전 확인"
규칙이 테스트 실행으로 우회된다.

여기서 고정하는 계약:
  1. 초안(`status: draft`)은 **채점에 들어가지 않는다** (D-039)
  2. 필드마다 채점 방식이 다르고, 자유 태그는 임계값 판정에서 빠진다 (D-040)
  3. judge 호출이 실패해도 **대조 결과는 살아남는다**
  4. 잴 수 없었던 지표는 0 이 아니라 None 이다
"""

from __future__ import annotations

import json

import pytest

from eval.runner import (
    JUDGE_PROMPT_DIR,
    EvalError,
    build_judge_variables,
    compare_ontology,
    evaluate_item,
    judge_client_from_config,
    load_golden_set,
    load_judge_prompt,
    load_predictions,
    new_run_id,
    prompt_sha256,
    summarize,
    write_scores,
)
from eval.schema import GoldenItem, HumanSummaryScores, ItemScore, SummaryJudgement
from extraction.llm import LLMClient, StructuredResult, Usage
from extraction.schema import NewsOntology

GOLDEN_SET_README = (
    __import__("pathlib").Path(__file__).resolve().parent.parent
    / "eval" / "golden_set" / "README.md"
)

# ---------------------------------------------------------------------------
# 픽스처
# ---------------------------------------------------------------------------
GOLDEN_PAYLOAD = {
    "id": "20260829-geeknews-33001-ffmpeg",
    "source_url": "https://news.hada.io/topic?id=33001",
    "status": "confirmed",
    "input": {
        "제목": "바이브코딩으로 만든 퍼저가 FFmpeg의 0 나누기 버그를 발견",
        "본문": "FFmpeg의 Sony PS2 VPK 디먹서에서 21바이트 입력으로 재현되는 버그가 발견됨",
    },
    "expected": {
        "기술영역": ["Application/Product"],
        "발표유형": "Community/Discussion",
        "관련기업": ["FFmpeg"],
        "관련기존기술": ["Fuzzing", "Vibe Coding"],
        "영향도": {"점수": 2, "근거": "국소적 버그지만 AI 보조 퍼징의 실사용 사례다."},
    },
    "labeled_by": "clickshn",
    "labeled_at": "2026-08-30",
}

ONTOLOGY_PAYLOAD = {
    "요약": "FFmpeg의 Sony PS2 VPK 디먹서에서 0 나누기 버그가 발견됐다. LLM 보조로 만든 퍼저가 찾아낸 사례다.",
    "기술영역": ["Application/Product"],
    "발표유형": "Community/Discussion",
    "관련기업": [{"원문표기": "FFmpeg", "역할": "영향 대상"}],
    "관련기존기술": ["Fuzzing", "Vibe Coding"],
    "영향도": {"점수": 2, "근거": "파급은 국소적이나 AI 보조 퍼징의 실제 적용 사례다."},
}

JUDGEMENT_PAYLOAD = {
    "faithfulness": {"rationale": "모든 문장이 원문의 디먹서 설명으로 검증된다", "score": 5},
    "completeness": {"rationale": "무엇이 누구에게 일어났는지가 모두 담겼다", "score": 4},
    "concision": {"rationale": "두 문장이고 군더더기가 없다", "score": 5},
    "unsupported_claims": [],
}


@pytest.fixture
def golden() -> GoldenItem:
    return GoldenItem.model_validate(GOLDEN_PAYLOAD)


@pytest.fixture
def ontology() -> NewsOntology:
    return NewsOntology.model_validate(ONTOLOGY_PAYLOAD)


class FakeJudge:
    """LLMClient Protocol 을 만족하는 가짜. 호출 인자를 기록한다."""

    model = "fake-judge"

    def __init__(self, payload: dict | None = None, *, error: Exception | None = None) -> None:
        self.payload = payload or JUDGEMENT_PAYLOAD
        self.error = error
        self.calls: list[dict] = []

    def parse_into(self, *, system, user, output_model):
        self.calls.append({"system": system, "user": user, "output_model": output_model})
        if self.error is not None:
            raise self.error
        return StructuredResult(
            value=output_model.model_validate(self.payload),
            usage=Usage(input_tokens=900, output_tokens=120, model=self.model),
        )


def write_golden(directory, payload: dict, name: str = "item.json") -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / name).write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def field(scores, name):
    return next(s for s in scores if s.field == name)


# ---------------------------------------------------------------------------
# 1. 골든셋 로딩 — 초안은 채점에 들어가지 않는다 (D-039)
# ---------------------------------------------------------------------------
def test_confirmed_item_is_loaded(tmp_path):
    write_golden(tmp_path, GOLDEN_PAYLOAD)
    assert [i.id for i in load_golden_set(tmp_path)] == [GOLDEN_PAYLOAD["id"]]


def test_draft_item_is_skipped_by_default(tmp_path, capsys):
    write_golden(tmp_path, {**GOLDEN_PAYLOAD, "status": "draft"})
    assert load_golden_set(tmp_path) == []
    assert "초안" in capsys.readouterr().err


def test_draft_can_be_loaded_explicitly(tmp_path):
    write_golden(tmp_path, {**GOLDEN_PAYLOAD, "status": "draft"})
    assert len(load_golden_set(tmp_path, include_drafts=True)) == 1


def test_template_file_is_not_loaded(tmp_path):
    write_golden(tmp_path, GOLDEN_PAYLOAD, name="TEMPLATE.json")
    assert load_golden_set(tmp_path, include_drafts=True) == []


def test_broken_file_stops_the_run(tmp_path):
    """부분 로드는 accuracy 를 거짓말하게 만든다 — 조용히 넘기지 않는다."""
    write_golden(tmp_path, GOLDEN_PAYLOAD)
    (tmp_path / "broken.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(EvalError, match="broken.json"):
        load_golden_set(tmp_path)


def test_repo_golden_set_loads():
    """실제 eval/golden_set/ 이 스키마에 맞고 confirmed 로 로드되는지.

    #33001 은 2026-08-30 에 사람이 확정했다. `confirmed` 승격 조건(영향도·본문·
    labeler·플레이스홀더 없음)을 실제 파일로도 고정해 둔다.
    """
    items = load_golden_set()
    # 확정 항목은 자산이므로 **조용히 줄거나 바뀌면 알아채야 한다.** 새 항목을
    # 추가할 때는 이 목록도 함께 늘린다.
    assert [i.id for i in items] == [
        "20260829-geeknews-33001-ffmpeg",
        "20260831-arxiv-2608.31100-s3gym",
    ]

    for item in items:
        assert item.expected.impact is not None, item.id
        assert item.input.body, f"{item.id}: 본문이 비면 재추출·judge 채점을 재현할 수 없다"
        assert item.labeled_by and item.labeled_at, item.id
        assert item.human_summary_scores is not None, f"{item.id}: 사람 점수가 있어야 judge 편향을 잰다"


def test_repo_second_item_labeled_under_v3_guideline():
    """S3Gym 은 v3 4슬롯 기준으로 사람이 라벨링한 첫 항목이다.

    #33001 은 `pre-2026-08-30` 이라 사람과 judge 가 다른 눈금을 썼다. 이 항목이
    **같은 눈금을 쓴 유일한 항목**이라는 사실이 판단의 전제이므로 고정해 둔다.
    사람이 모델 초안(기술영역 3개)을 2개로 줄인 것도 함께 잡는다 — 초안을 그대로
    승격한 것이 아니라는 증거다.
    """
    item = {i.id: i for i in load_golden_set()}["20260831-arxiv-2608.31100-s3gym"]
    assert item.labeling_guideline == "post-2026-08-30"
    assert [d.value for d in item.expected.tech_domains] == ["Agent", "Eval/Governance"]
    assert item.human_summary_scores.completeness == 5


def test_repo_golden_set_human_score_differs_from_model():
    """사람이 모델과 다르게 라벨한 것이 실제로 남아 있는지.

    영향도를 모델은 2, 사람은 1로 봤다. 이 불일치가 채점기에 잡히는 것이
    골든셋을 두는 이유이므로, 값이 조용히 같아지면 알아채야 한다.
    """
    items = {i.id: i for i in load_golden_set()}
    item = items["20260829-geeknews-33001-ffmpeg"]
    assert item.expected.impact.score == 1
    assert item.human_summary_scores.completeness == 1


# ---------------------------------------------------------------------------
# 2. confirmed 승격 조건 — 빈칸을 정답으로 쓰지 않는다
# ---------------------------------------------------------------------------
def test_confirmed_requires_impact():
    payload = {**GOLDEN_PAYLOAD, "expected": {**GOLDEN_PAYLOAD["expected"], "영향도": None}}
    with pytest.raises(ValueError, match="영향도"):
        GoldenItem.model_validate(payload)


@pytest.mark.parametrize("missing", ["labeled_by", "labeled_at"])
def test_confirmed_requires_labeler(missing):
    with pytest.raises(ValueError, match=missing):
        GoldenItem.model_validate({**GOLDEN_PAYLOAD, missing: None})


def test_confirmed_requires_body():
    payload = {**GOLDEN_PAYLOAD, "input": {**GOLDEN_PAYLOAD["input"], "본문": ""}}
    with pytest.raises(ValueError, match="본문"):
        GoldenItem.model_validate(payload)


def test_confirmed_rejects_leftover_placeholder():
    payload = {**GOLDEN_PAYLOAD, "labeled_by": "<<채워주세요>>"}
    with pytest.raises(ValueError, match="채워주세요"):
        GoldenItem.model_validate(payload)


def test_draft_may_be_incomplete():
    """초안 단계에서는 빈칸이 정상이다 — 그게 초안의 목적이다."""
    payload = {
        **GOLDEN_PAYLOAD,
        "status": "draft",
        "labeled_by": "<<채워주세요>>",
        "labeled_at": None,
        "expected": {**GOLDEN_PAYLOAD["expected"], "영향도": None},
    }
    assert GoldenItem.model_validate(payload).status == "draft"


# ---------------------------------------------------------------------------
# 3. 필드 대조 (D-040)
# ---------------------------------------------------------------------------
def test_perfect_match_scores_one(golden, ontology):
    scores = compare_ontology(golden, ontology)
    assert all(s.score == 1.0 for s in scores)


def test_release_type_is_all_or_nothing(golden):
    wrong = NewsOntology.model_validate({**ONTOLOGY_PAYLOAD, "발표유형": "Paper"})
    assert field(compare_ontology(golden, wrong), "발표유형").score == 0.0


def test_tech_domain_gives_partial_credit(golden):
    """2/3 맞춘 것과 0/3 이 같은 점수면 추이를 볼 수 없다."""
    payload = {**ONTOLOGY_PAYLOAD, "기술영역": ["Application/Product", "Agent"]}
    score = field(compare_ontology(golden, NewsOntology.model_validate(payload)), "기술영역")
    assert score.score == 0.5      # 교집합 1 / 합집합 2
    assert score.method == "jaccard"


def test_tech_domain_completely_wrong_scores_zero(golden):
    payload = {**ONTOLOGY_PAYLOAD, "기술영역": ["RAG"]}
    assert field(compare_ontology(golden, NewsOntology.model_validate(payload)), "기술영역").score == 0.0


@pytest.mark.parametrize(
    ("actual_score", "expected_points"),
    [(2, 1.0), (3, 0.5), (1, 0.5), (4, 0.0), (5, 0.0)],
)
def test_impact_is_ordinal_not_categorical(golden, actual_score, expected_points):
    """1~5 순서형이라 2 vs 3 과 2 vs 5 를 같게 볼 수 없다."""
    payload = {**ONTOLOGY_PAYLOAD, "영향도": {"점수": actual_score, "근거": "채점 테스트를 위한 근거 문장이다."}}
    score = field(compare_ontology(golden, NewsOntology.model_validate(payload)), "영향도")
    assert score.score == expected_points
    assert f"절대오차 {abs(2 - actual_score)}" in score.note


def test_impact_without_answer_is_not_graded(golden, ontology):
    """정답이 비어 있으면 0 점이 아니라 채점 제외다."""
    draft = golden.model_copy(update={"status": "draft"})
    draft.expected.impact = None
    score = field(compare_ontology(draft, ontology), "영향도")
    assert score.graded is False
    assert "채점하지 않았다" in score.note


def test_free_tags_are_recorded_but_not_graded(golden, ontology):
    for name in ("관련기존기술", "관련기업"):
        assert field(compare_ontology(golden, ontology), name).graded is False


def test_company_comparison_uses_canonical(golden):
    """원문표기가 달라도 정규화 후 같으면 맞은 것이다."""
    payload = {**ONTOLOGY_PAYLOAD, "관련기업": [{"원문표기": "ffmpeg"}]}
    score = field(compare_ontology(golden, NewsOntology.model_validate(payload)), "관련기업")
    assert score.score == 1.0


def test_prior_art_ignores_case_and_spacing(golden):
    payload = {**ONTOLOGY_PAYLOAD, "관련기존기술": ["fuzzing", "vibecoding"]}
    score = field(compare_ontology(golden, NewsOntology.model_validate(payload)), "관련기존기술")
    assert score.score == 1.0


def test_compare_makes_no_api_call(golden, ontology):
    """대조는 순수 함수다 — 클라이언트를 아예 받지 않는다."""
    import inspect

    assert "client" not in inspect.signature(compare_ontology).parameters


# ---------------------------------------------------------------------------
# 4. field_accuracy — 채점 대상만 평균낸다
# ---------------------------------------------------------------------------
def test_field_accuracy_excludes_ungraded(golden, ontology):
    payload = {**ONTOLOGY_PAYLOAD, "관련기존기술": ["전혀", "다른", "태그"]}
    score = evaluate_item(golden, NewsOntology.model_validate(payload))
    # 자유 태그가 0점이어도 채점 대상 3개는 만점이다.
    assert score.field_accuracy == 1.0
    assert len(score.graded_fields) == 3


def test_field_accuracy_is_none_when_nothing_graded(golden, ontology):
    draft = golden.model_copy(update={"status": "draft"})
    draft.expected.impact = None
    score = ItemScore(
        item_id="x",
        source_url="u",
        field_scores=[s for s in compare_ontology(draft, ontology) if not s.graded],
    )
    assert score.field_accuracy is None


# ---------------------------------------------------------------------------
# 5. judge — 호출·파싱
# ---------------------------------------------------------------------------
def test_judge_is_not_called_without_a_client(golden, ontology):
    """기본값이 '호출 안 함' 이어야 채점기를 돌리는 것만으로 비용이 나지 않는다."""
    score = evaluate_item(golden, ontology)
    assert score.judgement is None
    assert score.metadata.judge_model is None


def test_judge_result_is_parsed(golden, ontology):
    judge = FakeJudge()
    score = evaluate_item(golden, ontology, judge_client=judge)

    assert score.judgement.faithfulness.score == 5
    assert score.judgement.mean_score == pytest.approx(14 / 3, abs=1e-3)
    assert judge.calls[0]["output_model"] is SummaryJudgement
    assert score.metadata.judge_model == "fake-judge"
    assert score.metadata.judge_prompt == "summary_quality.v3.md"
    assert score.metadata.judge_prompt_sha256


def test_judge_sees_source_and_summary(golden, ontology):
    judge = FakeJudge()
    evaluate_item(golden, ontology, judge_client=judge)

    user = judge.calls[0]["user"]
    assert golden.input.title in user
    assert golden.input.body in user
    assert ontology.summary in user
    assert "{{" not in user and "{{" not in judge.calls[0]["system"]


def test_judge_failure_keeps_field_scores(golden, ontology):
    """요약 채점을 못 했다고 필드 정확도까지 잃을 이유가 없다."""
    score = evaluate_item(golden, ontology, judge_client=FakeJudge(error=RuntimeError("429")))

    assert score.judgement is None
    assert score.field_accuracy == 1.0
    assert any("judge 실패" in e and "429" in e for e in score.errors)


def test_rationale_comes_before_score_in_schema():
    """근거를 먼저 쓰게 하는 루브릭을 스키마 필드 순서로도 강제한다."""
    from eval.schema import Criterion

    assert list(Criterion.model_fields) == ["rationale", "score"]


def test_missing_human_scores_is_recorded_as_unverifiable(golden, ontology):
    score = evaluate_item(golden, ontology, judge_client=FakeJudge())
    assert any("자기 편향" in e for e in score.errors)
    assert score.judge_human_gap is None


def test_judge_human_gap_is_computed_when_available(golden, ontology):
    labeled = golden.model_copy(
        update={
            "human_summary_scores": HumanSummaryScores.model_validate(
                {"충실성": 4, "완결성": 4, "간결성": 4}
            )
        }
    )
    score = evaluate_item(labeled, ontology, judge_client=FakeJudge())
    assert score.judge_human_gap == pytest.approx(14 / 3 - 4, abs=1e-3)


def test_active_judge_prompt_is_v3():
    from eval.runner import DEFAULT_JUDGE_PROMPT

    assert DEFAULT_JUDGE_PROMPT == "summary_quality.v3.md"


@pytest.mark.parametrize(
    ("filename", "must_contain"),
    [
        ("summary_quality.v1.md", "아래 JSON 만 출력한다"),   # v1 고유
        ("summary_quality.v2.md", "2~3문장"),                # v2 가 고친 규칙
        ("summary_quality.v3.md", "슬롯"),                   # v3 가 넣은 규칙
    ],
)
def test_every_judge_prompt_version_is_preserved(filename, must_contain):
    """실행에 쓰인 버전은 지우지도 고치지도 않는다 (D-010 / D-023 / D-041 / D-045).

    v2 는 2026-08-30 첫 실행에 실제로 쓰였으므로, 그 점수가 어떤 지시에서 나왔는지
    추적 가능해야 한다.
    """
    prompt = load_judge_prompt(filename)
    assert prompt.system and prompt.user
    assert must_contain in prompt.system + prompt.user


def test_later_rules_did_not_leak_into_earlier_versions():
    """새 버전의 규칙이 옛 버전에 새어들면 '보존'이 아니다."""
    v1 = load_judge_prompt("summary_quality.v1.md")
    v2 = load_judge_prompt("summary_quality.v2.md")
    v1_text = v1.system + v1.user
    v2_text = v2.system + v2.user

    assert "2~3문장" not in v1_text, "v2 의 규칙이 v1 에 들어갔다"
    assert "슬롯" not in v1_text and "슬롯" not in v2_text, "v3 의 규칙이 옛 버전에 들어갔다"


# ---------------------------------------------------------------------------
# v3 완결성 세분화 (D-045)
# ---------------------------------------------------------------------------
def test_v3_defines_what_counts_as_core():
    """구간만 나누고 '핵심'을 정의하지 않으면 채점자마다 다른 것을 센다.

    첫 실행에서 judge 와 사람의 누락 목록은 합집합 6개 중 1개만 겹쳤다 —
    점수 차이의 원인이 관대함이 아니라 채점 대상 불일치였다.
    """
    system = load_judge_prompt().system
    for slot in ["① 사건", "② 범위", "③ 정도", "④ 경위"]:
        assert slot in system, f"슬롯 정의에 {slot} 가 없다"


def test_v3_completeness_bands_are_granular():
    """v2 는 '하나가 빠졌다=3'이 사실상 하한이라 2개든 5개든 3점으로 수렴했다."""
    system = load_judge_prompt().system
    for band in ["4슬롯 모두", "3슬롯을 채웠다", "2슬롯을 채웠다", "1슬롯만 채웠다"]:
        assert band in system, f"구간 '{band}' 가 없다"


def test_v3_rejects_bare_mention_as_slot_fill():
    """슬롯을 절반만 채운 것을 충족으로 세면 구간을 나눈 의미가 없다."""
    system = load_judge_prompt().system
    assert "이름만 언급하고 구체가 없으면 미충족" in system


def test_v3_discounts_slots_the_source_never_provided():
    """원문이 잘려서 없는 정보를 요약 탓으로 돌리면 안 된다 (D-013)."""
    assert "분모에서 뺀다" in load_judge_prompt().system


def test_v3_carries_the_boundary_case():
    """왜 이렇게 나눴는지를 다음 검토자가 바로 알 수 있어야 한다."""
    system = load_judge_prompt().system
    assert "경계 사례" in system
    assert "495,211회" in system, "실제 사례의 구체 수치가 있어야 한다"
    assert "1개만 겹쳤다" in system, "판단 근거가 된 관측이 있어야 한다"


def test_v3_keeps_faithfulness_and_concision_unchanged():
    """완결성만 고쳤다 — 나머지 축을 조용히 바꾸면 점수 추이를 못 읽는다."""
    v2, v3 = load_judge_prompt("summary_quality.v2.md"), load_judge_prompt()
    for rule in [
        "5: 모든 문장이 원문으로 검증된다.",
        "5: 2~3문장, 모든 문장이 정보를 더한다.",
    ]:
        assert rule in v2.system and rule in v3.system


def test_v3_marks_itself_unverified():
    """루브릭 변경은 가설이다. 재실행 전까지 효과를 단정하지 않는다 (D-042)."""
    raw = (JUDGE_PROMPT_DIR / "summary_quality.v3.md").read_text(encoding="utf-8")
    assert "아직 검증되지 않았다" in raw


# ---------------------------------------------------------------------------
# 라벨링 가이드 (D-046)
# ---------------------------------------------------------------------------
def test_labeling_guide_forbids_out_of_rubric_criteria():
    """규칙이 문서에 실제로 적혀 있는지. 합의만 하고 안 적으면 다음에 또 섞인다."""
    text = (GOLDEN_SET_README).read_text(encoding="utf-8")
    assert "루브릭 밖 기준을 섞지 않는다" in text
    for banned in ["직무 관련성", "개인적 관심사", "포트폴리오"]:
        assert banned in text, f"금지 기준 '{banned}' 가 명시되지 않았다"


def test_pre_guideline_item_is_marked_not_edited():
    """규칙 이전 라벨은 표시만 하고 점수·근거를 고치지 않는다 (D-046)."""
    item = load_golden_set()[0]
    assert item.labeling_guideline == "pre-2026-08-30"
    # 사람이 매긴 값이 사후 수정되지 않았는지
    assert item.expected.impact.score == 1
    assert "AI 엔지니어링 직무 관련성" in item.expected.impact.rationale
    assert "사후 수정하지 않는다" in item.notes


def test_judge_prompt_v1_is_preserved():
    """v1 은 보존한다. v2 가 고친 규칙이 v1 에 새어들지 않았는지 (D-041)."""
    v1 = load_judge_prompt("summary_quality.v1.md")
    v2 = load_judge_prompt()
    # 루브릭 본문은 v1 이 User, v2 가 System 섹션에 있으므로 전체를 본다.
    v1_text, v2_text = v1.system + v1.user, v2.system + v2.user
    assert "3~5문장" in v1_text, "v1 의 옛 기준이 사라졌다 = v1 을 고쳤다는 뜻"
    assert "2~3문장" in v2_text and "3~5문장" not in v2_text
    assert "아래 JSON 만 출력한다" in v1_text
    assert "아래 JSON 만 출력한다" not in v2_text


def test_judge_variables_handle_empty_body(golden):
    golden.input.body = ""
    variables = build_judge_variables(golden, "요약문이다.")
    assert variables["source_text"] == golden.input.title


# ---------------------------------------------------------------------------
# 6. 집계 — 잴 수 없었던 지표는 0 이 아니라 None
# ---------------------------------------------------------------------------
def test_summary_averages_graded_accuracy(golden, ontology):
    wrong = NewsOntology.model_validate({**ONTOLOGY_PAYLOAD, "발표유형": "Paper"})
    scores = [evaluate_item(golden, ontology), evaluate_item(golden, wrong)]

    result = summarize(scores, run_id="r", thresholds={"field_accuracy": 0.90})
    # 1.0 과 (1 + 0 + 1)/3 의 평균
    assert result.field_accuracy == pytest.approx((1.0 + 2 / 3) / 2, abs=1e-3)
    assert result.passed["field_accuracy"] is False


def test_summary_without_judge_leaves_faithfulness_none(golden, ontology):
    result = summarize([evaluate_item(golden, ontology)], run_id="r",
                       thresholds={"summary_faithfulness": 4.0})
    assert result.summary_faithfulness is None
    assert "summary_faithfulness" not in result.passed
    assert any("judge 를 돌리지 않아" in n for n in result.notes)


def test_empty_run_is_flagged():
    result = summarize([], run_id="r")
    assert result.item_count == 0
    assert result.field_accuracy is None
    assert any("채점된 항목이 없다" in n for n in result.notes)


def test_threshold_passes_when_met(golden, ontology):
    result = summarize(
        [evaluate_item(golden, ontology, judge_client=FakeJudge())],
        run_id="r",
        thresholds={"field_accuracy": 0.90, "summary_faithfulness": 4.0},
    )
    assert result.passed == {"field_accuracy": True, "summary_faithfulness": True}


def test_repo_thresholds_are_readable():
    from eval.runner import load_config

    thresholds = (load_config().get("eval") or {}).get("thresholds") or {}
    assert thresholds["field_accuracy"] == 0.90
    assert thresholds["summary_faithfulness"] == 4.0


# ---------------------------------------------------------------------------
# 7. 저장
# ---------------------------------------------------------------------------
def test_scores_are_written_as_jsonl(tmp_path, golden, ontology):
    scores = [evaluate_item(golden, ontology, judge_client=FakeJudge())]
    summary = summarize(scores, run_id="20260830T120000Z")

    path = write_scores(scores, summary, scores_dir=tmp_path)

    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert path.name == "20260830T120000Z.jsonl"
    assert [r["type"] for r in rows] == ["item", "summary"]
    assert rows[0]["item_id"] == golden.id
    assert rows[-1]["item_count"] == 1


def test_scores_keep_korean_readable(tmp_path, golden, ontology):
    scores = [evaluate_item(golden, ontology, judge_client=FakeJudge())]
    path = write_scores(scores, summarize(scores, run_id="r"), scores_dir=tmp_path)
    assert "\\u" not in path.read_text(encoding="utf-8")


def test_write_leaves_no_temp_file(tmp_path, golden, ontology):
    scores = [evaluate_item(golden, ontology)]
    write_scores(scores, summarize(scores, run_id="r"), scores_dir=tmp_path)
    assert list(tmp_path.glob(".tmp-*")) == []


def test_scores_record_reproduction_metadata(tmp_path, golden, ontology):
    """프롬프트가 바뀌면 점수도 바뀐다 — 어떤 조건이었는지 남아야 한다 (D-010)."""
    score = evaluate_item(
        golden, ontology, judge_client=FakeJudge(),
        extraction_model="claude-opus-5", extraction_prompt="extract_ontology.v3.md",
    )
    meta = score.metadata
    assert meta.extraction_prompt == "extract_ontology.v3.md"
    assert meta.extraction_prompt_sha256
    assert meta.judge_prompt_sha256 == prompt_sha256("summary_quality.v3.md")
    assert meta.evaluated_at.tzinfo is not None


def test_run_id_is_sortable():
    assert new_run_id() < new_run_id(__import__("datetime").datetime(2099, 1, 1))


# ---------------------------------------------------------------------------
# 8. 예측 파일
# ---------------------------------------------------------------------------
def test_predictions_round_trip(tmp_path, ontology):
    path = tmp_path / "preds.jsonl"
    path.write_text(
        json.dumps(
            {"item_id": "x", "ontology": ontology.model_dump(by_alias=True, mode="json")},
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    loaded = load_predictions(path)
    assert loaded["x"].release_type == ontology.release_type


def test_broken_prediction_file_raises(tmp_path):
    path = tmp_path / "preds.jsonl"
    path.write_text('{"item_id": "x"}\n', encoding="utf-8")
    with pytest.raises(EvalError):
        load_predictions(path)


# ---------------------------------------------------------------------------
# 9. judge 클라이언트 설정
# ---------------------------------------------------------------------------
def test_judge_client_reads_eval_judge_model():
    client = judge_client_from_config(
        {"eval": {"judge_model": "claude-opus-5"}}, client=object()
    )
    assert client.model == "claude-opus-5"


def test_judge_client_sends_no_temperature():
    """Opus 5 는 temperature 를 400 으로 거부한다 (D-033)."""
    client = judge_client_from_config({"eval": {"judge_model": "claude-opus-5"}}, client=object())
    assert "extra_body" not in client._request_kwargs()


def test_fake_judge_satisfies_protocol():
    assert isinstance(FakeJudge(), LLMClient)
