"""eval 레이어 스키마 — 골든셋 항목, 채점 결과, judge 출력.

`extraction/schema.py` 가 온톨로지의 SSoT 이듯 이 파일은 **채점의 SSoT** 다.
통제어휘는 여기서 다시 정의하지 않고 `extraction.schema` 의 Enum 을 그대로
가져다 쓴다 — 어휘 미러를 하나 더 만들면 D-002 가 무너진다.

## 골든셋은 두 상태를 가진다 (D-039)

`status: draft` 는 **모델 출력을 옮겨 적은 검토용 초안**이고, `confirmed` 는
사람이 라벨을 확정한 정답이다. 채점기는 기본적으로 `confirmed` 만 읽는다.
모델 출력을 그대로 정답으로 삼으면 eval 이 "모델이 스스로를 채점하는" 구조가
되기 때문이다(`eval/golden_set/README.md`). 이 구분을 주석이 아니라 **스키마와
로더로** 강제해서, 초안이 실수로 채점에 섞이는 경로를 없앤다.

미기입 자리는 `<<채워주세요>>` 로 표시한다. `confirmed` 로 올릴 때 이 문자열이
남아 있으면 검증에서 걸린다.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from extraction.schema import ReleaseType, TechDomain

# 사람이 채워야 할 자리. 파일을 열었을 때 눈에 띄고, 검증에서 걸린다.
NEEDS_FILL = "<<채워주세요>>"

Score1to5 = Annotated[int, Field(ge=1, le=5)]


def _contains_placeholder(value: Any) -> bool:
    """중첩 구조 어디든 `NEEDS_FILL` 이 남아 있는지."""
    if isinstance(value, str):
        return NEEDS_FILL in value
    if isinstance(value, dict):
        return any(_contains_placeholder(v) for v in value.values())
    if isinstance(value, (list, tuple)):
        return any(_contains_placeholder(v) for v in value)
    return False


# ---------------------------------------------------------------------------
# 골든셋
# ---------------------------------------------------------------------------
class GoldenInput(BaseModel):
    """채점 대상 추출을 재현하는 데 필요한 원문.

    수집 파이프라인은 `RawItem` 본문을 어디에도 보존하지 않는다(`data/` 는
    gitignore 대상이고 그런 저장 단계 자체가 없다). 그래서 골든셋이 **원문의
    유일한 사본**이다 — 여기서 본문이 비면 나중에 재추출로 대조할 수 없다.
    """

    model_config = ConfigDict(populate_by_name=True)

    title: str = Field(alias="제목", min_length=1)
    body: str = Field(alias="본문", default="")


class GoldenImpact(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    score: Score1to5 = Field(alias="점수")
    rationale: str = Field(alias="근거", min_length=1)


class GoldenExpectation(BaseModel):
    """사람이 정한 정답 라벨.

    `관련기업` 은 `CompanyRef` 객체가 아니라 **대표명 문자열 목록**이다.
    라벨링하는 사람이 채워야 하는 것은 "어느 기업이 관련되는가"이지 정규화
    성공 여부가 아니고, 그건 `normalize.py` 가 결정적으로 계산한다(D-026).
    """

    model_config = ConfigDict(populate_by_name=True)

    tech_domains: list[TechDomain] = Field(alias="기술영역", min_length=1, max_length=3)
    release_type: ReleaseType = Field(alias="발표유형")
    companies: list[str] = Field(alias="관련기업", default_factory=list)
    prior_art: list[str] = Field(alias="관련기존기술", default_factory=list)
    impact: GoldenImpact | None = Field(alias="영향도", default=None)


class HumanSummaryScores(BaseModel):
    """사람이 매긴 요약 점수. judge 와의 일치도를 재기 위한 기준선.

    judge 와 extractor 가 같은 모델이라 자기 편향이 의심되는데(eval/README),
    **사람 점수가 없으면 그 의심을 확인할 방법이 없다.** 필수는 아니지만
    없으면 채점 결과에 "편향 확인 불가"로 남는다.
    """

    model_config = ConfigDict(populate_by_name=True)

    faithfulness: Score1to5 = Field(alias="충실성")
    completeness: Score1to5 = Field(alias="완결성")
    concision: Score1to5 = Field(alias="간결성")


class GoldenItem(BaseModel):
    """골든셋 1건."""

    model_config = ConfigDict(populate_by_name=True)

    id: str = Field(min_length=1)
    source_url: str = Field(min_length=1)
    status: Literal["draft", "confirmed"] = "draft"
    input: GoldenInput
    expected: GoldenExpectation
    reference_summary: str | None = None
    human_summary_scores: HumanSummaryScores | None = None
    labeled_by: str | None = None
    labeled_at: date | None = None
    notes: str = ""
    # 초안이 어디서 왔는지. 확정 시 "무엇을 사람이 바꿨는지" 대조에 쓴다.
    draft_source: str | None = None
    # 어떤 라벨링 규칙 아래에서 매긴 라벨인지. 프롬프트 버전을 기록하는 것과
    # 같은 이유다(D-010) — 규칙이 바뀌면 라벨의 의미도 바뀌는데, 어느 규칙에서
    # 나온 라벨인지 모르면 나중에 재라벨링 대상을 고를 수 없다. (D-046)
    labeling_guideline: str | None = None

    @model_validator(mode="after")
    def _confirmed_items_must_be_complete(self) -> "GoldenItem":
        """`confirmed` 로 올리는 순간부터 빈칸을 허용하지 않는다.

        초안 단계에서는 비어 있어도 되지만, 정답으로 쓰이는 순간 빠진 필드는
        조용한 오답이 된다 — 채점기가 "정답이 없음"과 "정답이 X"를 구분하지
        못하면 accuracy 가 의미를 잃는다.
        """
        if self.status != "confirmed":
            return self

        missing: list[str] = []
        if self.expected.impact is None:
            missing.append("expected.영향도")
        if not self.input.body:
            missing.append("input.본문")
        if not self.labeled_by:
            missing.append("labeled_by")
        if self.labeled_at is None:
            missing.append("labeled_at")
        if missing:
            raise ValueError(
                f"status='confirmed' 인데 비어 있는 필드: {', '.join(missing)}. "
                "확정 전이라면 status 를 'draft' 로 두세요."
            )

        if _contains_placeholder(self.model_dump(mode="json")):
            raise ValueError(
                f"status='confirmed' 인데 {NEEDS_FILL!r} 자리가 남아 있습니다."
            )
        return self


# ---------------------------------------------------------------------------
# judge 출력
# ---------------------------------------------------------------------------
class Criterion(BaseModel):
    """채점 축 하나. **근거가 점수보다 먼저 온다.**

    필드 순서가 곧 생성 순서다. 점수를 먼저 내게 하면 모델이 점수를 정해 놓고
    근거를 지어낸다 — 루브릭이 "근거를 먼저 쓴 뒤 점수를 낸다"고 요구하는 것을
    스키마 층에서도 같은 순서로 강제한다.
    """

    rationale: str = Field(description="한 문장. 원문의 어느 부분을 근거로 삼았는지 밝힌다", min_length=5)
    score: Score1to5 = Field(description="루브릭 구간에 따른 1~5 점수")


class SummaryJudgement(BaseModel):
    """`summary_quality` 루브릭의 채점 결과."""

    faithfulness: Criterion = Field(description="원문에 없는 내용을 지어내지 않았는가")
    completeness: Criterion = Field(description="핵심을 빠뜨리지 않았는가")
    concision: Criterion = Field(description="분량 안에서 군더더기가 없는가")
    unsupported_claims: list[str] = Field(
        default_factory=list,
        description="원문에서 확인되지 않는 문장을 요약에서 그대로 인용. 없으면 빈 목록",
    )

    @property
    def mean_score(self) -> float:
        return round(
            (self.faithfulness.score + self.completeness.score + self.concision.score) / 3, 3
        )


# ---------------------------------------------------------------------------
# 채점 결과
# ---------------------------------------------------------------------------
class FieldScore(BaseModel):
    """필드 1개의 대조 결과.

    `score` 는 0.0~1.0 이고, 필드마다 계산 방식이 다르다(D-040). `graded` 가
    False 면 임계값 판정에서 빠지고 기록만 된다 — 자유 태그처럼 정답 집합이
    닫히지 않는 필드가 여기 해당한다.
    """

    field: str
    expected: Any
    actual: Any
    score: float = Field(ge=0.0, le=1.0)
    method: str
    graded: bool = True
    note: str = ""

    @property
    def exact(self) -> bool:
        return self.score == 1.0


class RunMetadata(BaseModel):
    """이 점수가 어떤 조건에서 나왔는지.

    프롬프트가 바뀌면 점수도 바뀐다(D-010). 어떤 프롬프트·모델에서 나온
    점수인지 모르면 추이를 비교할 수 없으므로, 파일명과 **해시**를 함께 남긴다
    (`eval/judge_prompts/README.md`).
    """

    evaluated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    extraction_model: str | None = None
    extraction_prompt: str | None = None
    extraction_prompt_sha256: str | None = None
    judge_model: str | None = None
    judge_prompt: str | None = None
    judge_prompt_sha256: str | None = None


class JudgeUsage(BaseModel):
    """judge 호출 1회의 사용량.

    `thinking_tokens` 는 `output_tokens` 의 **부분집합**이다 — 비용을 계산할 때
    두 값을 더하면 안 된다 (D-019).
    """

    input_tokens: int | None = None
    output_tokens: int | None = None
    thinking_tokens: int | None = None
    cache_read_input_tokens: int | None = None
    latency_s: float | None = None


class ItemScore(BaseModel):
    """골든셋 1건에 대한 채점 결과. `eval/scores/*.jsonl` 한 줄이 이 모양이다."""

    item_id: str
    source_url: str
    repeat_index: int = Field(
        default=0,
        description="같은 항목을 반복 채점할 때의 회차(0부터). 반복하지 않으면 0",
    )
    field_scores: list[FieldScore]
    judgement: SummaryJudgement | None = None
    judge_usage: JudgeUsage | None = None
    human_summary_scores: HumanSummaryScores | None = None
    metadata: RunMetadata = Field(default_factory=RunMetadata)
    errors: list[str] = Field(default_factory=list)

    @property
    def graded_fields(self) -> list[FieldScore]:
        return [f for f in self.field_scores if f.graded]

    @property
    def field_accuracy(self) -> float | None:
        """채점 대상 필드 점수의 평균. 대상이 없으면 None."""
        graded = self.graded_fields
        if not graded:
            return None
        return round(sum(f.score for f in graded) / len(graded), 4)

    @property
    def judge_human_gap(self) -> float | None:
        """judge 평균 - 사람 평균. 자기 편향을 보는 1차 지표.

        사람 점수가 없으면 None — "편향이 없다"가 아니라 **확인할 수 없다**는 뜻이다.
        """
        if self.judgement is None or self.human_summary_scores is None:
            return None
        h = self.human_summary_scores
        human_mean = (h.faithfulness + h.completeness + h.concision) / 3
        return round(self.judgement.mean_score - human_mean, 3)


class AxisStats(BaseModel):
    """채점 축 하나를 N회 반복했을 때의 분포.

    judge 는 결정적이지 않다(D-042). 평균만 남기면 "3.0"이 매번 3인지 1과 5를
    오간 결과인지 구분할 수 없다 — **흔들림 폭이 이 프로젝트에서 재려는 값**이므로
    표준편차·최빈값·전체 도수를 함께 남긴다.

    `stdev` 는 표본표준편차이고 `n < 2` 면 None 이다. 1회 관측에서 편차를 0 으로
    적으면 "흔들리지 않았다"로 읽히는데, 그건 재지 못한 것이다.
    """

    axis: str
    n: int
    mean: float
    stdev: float | None = None
    mode: int | None = None
    minimum: int
    maximum: int
    distribution: dict[str, int] = Field(
        default_factory=dict, description="점수 -> 관측 횟수. 키는 '1'~'5' 문자열"
    )

    @property
    def spread(self) -> int:
        """최대-최소. 0 이면 N회가 모두 같은 점수였다는 뜻이다."""
        return self.maximum - self.minimum

    @property
    def mode_ratio(self) -> float:
        """최빈값이 차지한 비율. 중심이 얼마나 뭉쳐 있는지."""
        if self.n == 0 or self.mode is None:
            return 0.0
        return round(self.distribution.get(str(self.mode), 0) / self.n, 3)


class RepeatStats(BaseModel):
    """항목 1건을 N회 반복 채점한 결과의 집계.

    `slot_adherence` 는 **점수와 독립적인 과정 검사**다. v3 루브릭은 근거에
    슬롯별 충족 여부를 밝히라고 요구하는데(D-045), 모델이 그 지시를 따르지
    않았다면 점수가 몇이든 "루브릭이 틀렸다"가 아니라 **"루브릭이 적용되지
    않았다"** 로 읽어야 한다. 두 경우의 후속 조치가 완전히 다르므로 갈라 둔다.
    """

    item_id: str
    judge_prompt: str | None = None
    n: int
    faithfulness: AxisStats | None = None
    completeness: AxisStats | None = None
    concision: AxisStats | None = None
    human_scores: HumanSummaryScores | None = None
    slot_adherence: float | None = Field(
        default=None,
        description="완결성 근거가 4슬롯을 모두 열거한 회차의 비율. 슬롯 개념이 없는 루브릭(v2 이하)에서는 None",
    )
    slot_fill_rate: dict[str, float] = Field(
        default_factory=dict,
        description="슬롯별로 '충족' 판정을 받은 회차의 비율. 점수가 아니라 판정 근거가 재현되는지를 본다",
    )
    failures: int = Field(default=0, description="judge 호출이 실패한 회차 수")

    def gap(self, axis: str) -> float | None:
        """축별 judge 평균 - 사람 점수. 사람 점수가 없으면 None."""
        stats = getattr(self, axis, None)
        if stats is None or self.human_scores is None:
            return None
        human = {
            "faithfulness": self.human_scores.faithfulness,
            "completeness": self.human_scores.completeness,
            "concision": self.human_scores.concision,
        }[axis]
        return round(stats.mean - human, 3)


class RunSummary(BaseModel):
    """실행 1회 전체의 집계. 임계값 판정이 여기서 난다."""

    run_id: str
    item_count: int = Field(description="채점한 **서로 다른** 골든셋 항목 수")
    judge_call_count: int = Field(default=0, description="실제로 성공한 judge 호출 수")
    repeat: int = Field(default=1, description="항목당 반복 채점 회차 수")
    repeat_stats: list[RepeatStats] = Field(default_factory=list)
    field_accuracy: float | None = None
    summary_faithfulness: float | None = None
    thresholds: dict[str, float] = Field(default_factory=dict)
    passed: dict[str, bool] = Field(default_factory=dict)
    notes: list[str] = Field(default_factory=list)
