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
    """실제 eval/golden_set/ 이 스키마에 맞는지 (초안 포함)."""
    items = load_golden_set(include_drafts=True)
    assert any(i.id == "20260829-geeknews-33001-ffmpeg" for i in items)
    assert load_golden_set() == [], "아직 confirmed 항목은 없어야 한다"


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
    assert score.metadata.judge_prompt == "summary_quality.v2.md"
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
    assert meta.judge_prompt_sha256 == prompt_sha256("summary_quality.v2.md")
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
