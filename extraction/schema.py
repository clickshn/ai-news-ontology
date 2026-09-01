"""온톨로지 스키마의 단일 출처(Single Source of Truth).

이 모듈은 `schema.md` 문서와 1:1로 대응한다. 둘 중 하나만 바꾸지 말 것.
- 통제어휘(고정 리스트)는 여기의 Enum 이 정본이고, `config.yaml` 의
  `ontology:` 블록은 사람이 읽기 위한 미러다. (README 결정 로그 D-002)
- 필드의 한국어 표기는 pydantic alias 로 둔다. 코드는 영어 필드명으로 다루고,
  Obsidian frontmatter 로 직렬화할 때만 `by_alias=True` 로 한국어 키를 쓴다.
  (README 결정 로그 D-003)

이번 단계에서는 "정의"만 둔다. LLM 호출/파싱/정규화 로직은 아직 넣지 않는다.
"""

from __future__ import annotations

from datetime import date
from enum import StrEnum
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, model_validator

from extraction.normalize import normalize_company


# ---------------------------------------------------------------------------
# 1. 기술영역 — 통제어휘 (고정 리스트)
# ---------------------------------------------------------------------------
class TechDomain(StrEnum):
    """기사/논문이 다루는 기술 영역. 폐쇄형 어휘.

    새 값을 추가하려면 config.yaml 의 미러와 schema.md 표도 함께 갱신한다.
    """

    LLM = "LLM"                              # 모델 자체(사전학습, 아키텍처, 신규 모델 공개)
    MULTIMODAL = "Multimodal"                # 비전/오디오/비디오 결합
    RAG = "RAG"                              # 검색 증강, 벡터DB, 문서 QA
    AGENT = "Agent"                          # 툴 사용, 자율 루프, 멀티에이전트
    REASONING = "Reasoning"                  # 추론/사고 시간 확장, CoT 계열
    TRAINING = "Training/Finetuning"         # 사후학습, RLHF/RLAIF, PEFT
    INFERENCE = "Inference/Serving"          # 서빙, 양자화, KV 캐시, 지연/처리량
    INFRA = "Infra/MLOps"                    # 오케스트레이션, 파이프라인, 관측
    EVAL = "Eval/Governance"                 # 벤치마크, 평가 방법론, 거버넌스
    SAFETY = "Safety/Alignment"              # 정렬, 레드팀, 해석가능성
    DATA = "Data/Synthetic"                  # 데이터셋, 합성데이터, 라이선스
    EMBODIED = "Embodied/Robotics"           # 로보틱스, 자율주행, 물리세계
    HARDWARE = "Hardware/Chip"               # GPU/TPU/NPU, 데이터센터
    APPLICATION = "Application/Product"      # 최종 사용자 제품/서비스 적용


# ---------------------------------------------------------------------------
# 2. 발표유형 — 통제어휘 (고정 리스트)
# ---------------------------------------------------------------------------
class ReleaseType(StrEnum):
    """소식의 형태. 정확히 하나만 고른다. 폐쇄형 어휘."""

    PAPER = "Paper"                          # 논문/프리프린트/기술 리포트
    PRODUCT_LAUNCH = "ProductLaunch"         # 상용 제품·기능·API 출시
    OPEN_SOURCE = "OpenSource"               # 가중치/코드/데이터 공개
    POLICY = "Policy/Regulation"             # 정책, 규제, 표준, 소송
    FUNDING = "Funding/M&A"                  # 투자 유치, 인수합병, 상장
    BENCHMARK = "Benchmark/Report"           # 공식 조직의 벤치마크 결과, 시장·산업 보고서
    COMMUNITY = "Community/Discussion"       # 커뮤니티·개인의 버그 발견, 이슈 제기, 사용기, 토론
    PARTNERSHIP = "Partnership/Contract"     # 조직 간 거래 관계의 체결·변경·종료 (공급, 제휴, 라이선싱)


# ---------------------------------------------------------------------------
# 3. 관련기업 — 자유 엔티티 + alias 정규화
# ---------------------------------------------------------------------------
class CompanyRef(BaseModel):
    """기사에 등장한 기업/기관 1건.

    자유 엔티티이므로 LLM 이 뽑은 원문 표기(`raw`)를 반드시 보존하고,
    `config.yaml` 의 `company_aliases` 로 정규화한 결과를 `canonical` 에 담는다.
    정규화에 실패하면 canonical == raw 이고 `resolved=False` 가 되며,
    observability 레이어가 이를 집계해 alias 목록 보강에 쓴다.
    """

    model_config = ConfigDict(populate_by_name=True)

    raw: str = Field(
        alias="원문표기",
        description="본문에 나타난 그대로의 표기. 예: '오픈AI', 'Open AI'",
        min_length=1,
    )
    canonical: str | None = Field(
        default=None,
        alias="정규명",
        description=(
            "alias 매핑으로 정규화된 대표명. 예: 'OpenAI'. "
            "**LLM 이 채우는 필드가 아니다** — normalize.py 가 채운다. "
            "입력값이 있어도 정규화 결과로 덮어쓴다."
        ),
    )
    resolved: bool = Field(
        default=False,
        alias="정규화성공",
        description="company_aliases 에서 매칭에 성공했는지 여부. normalize.py 가 설정한다",
    )
    role: str | None = Field(
        default=None,
        alias="역할",
        description="해당 소식에서의 역할. 예: '발표 주체', '인수 대상', '경쟁사'",
    )

    @model_validator(mode="after")
    def _normalize(self) -> "CompanyRef":
        """`raw` 로부터 `canonical` / `resolved` 를 **항상** 다시 계산한다.

        사전(`config.yaml: company_aliases`)이 이 두 필드의 단일 출처다.
        입력에 `정규명`·`정규화성공` 이 들어와도 무시하고 덮어쓴다 — 그래야
        같은 원문 표기가 언제나 같은 대표명으로 떨어진다. LLM 이 정규화까지
        하게 두면 결정적으로 처리할 수 있는 일을 확률적 모델에 맡기는 셈이다.
        (README 결정 로그 D-011 / D-025)
        """
        result = normalize_company(self.raw)
        object.__setattr__(self, "canonical", result.canonical)
        object.__setattr__(self, "resolved", result.resolved)
        return self


# ---------------------------------------------------------------------------
# 4. 영향도 — 1~5 리커트 + 근거 한 줄
# ---------------------------------------------------------------------------
ImpactScore = Annotated[int, Field(ge=1, le=5)]


class Impact(BaseModel):
    """영향도 평가. 점수만으로는 검증이 불가능하므로 근거를 강제한다.

    척도(자세한 기준은 schema.md 참고):
      1 점진적/국소적 · 2 특정 도메인 관심 · 3 업계 일반의 주목
      4 판도 변화 가능 · 5 패러다임 전환
    """

    model_config = ConfigDict(populate_by_name=True)

    score: ImpactScore = Field(alias="점수", description="1~5 리커트 척도")
    rationale: str = Field(
        alias="근거",
        description="점수를 그렇게 준 이유. 한 문장.",
        # min_length 는 공백을 포함한 전체 문자 수를 센다. 언어에 따라 의미가
        # 달라서(한국어 15자는 짧은 문장, 영어 15자는 두세 단어) 품질 기준이
        # 될 수 없다. 빈 문자열과 "없음" 류의 무응답만 막는 하한선이다.
        # 근거의 품질은 eval/ 의 judge 가 채점한다.
        min_length=15,
        max_length=300,
    )


# ---------------------------------------------------------------------------
# 5. 최종 레코드
# ---------------------------------------------------------------------------
class SourceRef(BaseModel):
    """수집 출처 메타데이터. collectors/ 가 채운다."""

    model_config = ConfigDict(populate_by_name=True)

    url: HttpUrl = Field(alias="출처")
    source_name: str = Field(alias="소스명", description="예: 'arXiv cs.CL', 'Anthropic News'")
    published_at: date | None = Field(default=None, alias="발행일")
    collected_at: date | None = Field(default=None, alias="수집일")


class NewsOntology(BaseModel):
    """온톨로지 5개 필드 = LLM 이 채워야 하는 구조.

    extraction/ 이 이 모델로 LLM 출력을 검증하고,
    obsidian_writer/ 가 `model_dump(by_alias=True)` 로 frontmatter 를 만든다.
    """

    model_config = ConfigDict(populate_by_name=True, use_enum_values=False)

    summary: str = Field(
        alias="요약",
        description=(
            "무슨 내용인지 2~3문장으로. 평서문('~다')으로 끝낸다. "
            "영향도 근거('왜 중요한가')와 역할이 다르다 — 여기엔 사실만 적는다."
        ),
        # 길이 제약은 빈 문자열과 한 단어 응답을 막는 하한선일 뿐 품질 기준이
        # 아니다. 언어별로 같은 글자 수의 정보량이 다르다 (Impact.rationale 참고).
        min_length=20,
        max_length=600,
    )
    tech_domains: list[TechDomain] = Field(
        alias="기술영역",
        description="가장 관련 깊은 영역 1~3개. 통제어휘 밖의 값은 허용하지 않는다.",
        min_length=1,
        max_length=3,
    )
    release_type: ReleaseType = Field(
        alias="발표유형",
        description="소식의 형태. 정확히 1개.",
    )
    companies: list[CompanyRef] = Field(
        default_factory=list,
        alias="관련기업",
        description="등장한 기업/기관. 없으면 빈 리스트.",
    )
    prior_art: list[str] = Field(
        default_factory=list,
        alias="관련기존기술",
        description=(
            "이 소식이 딛고 선 기존 기술/개념 자유 태그. 0~8개. "
            "표기는 영어 표준형으로 통일한다(예: 'Fuzzing', 'Vibe Coding'). "
            "한국어 병기·순한글 표기는 쓰지 않는다 — 전체 규칙은 schema.md "
            "'관련기존기술' 절 참고."
        ),
        max_length=8,
    )
    impact: Impact = Field(alias="영향도")


class NewsNote(BaseModel):
    """Obsidian 노트 1개에 대응하는 최종 산출물."""

    model_config = ConfigDict(populate_by_name=True)

    title: str = Field(alias="제목", min_length=1)
    summary: str = Field(
        alias="요약",
        description="3~5문장 한국어 요약. eval 의 채점 대상.",
        min_length=1,
    )
    ontology: NewsOntology = Field(alias="온톨로지")
    source: SourceRef = Field(alias="출처정보")


# ---------------------------------------------------------------------------
# 6. 관련성 게이트 (정밀 추출 앞단)
# ---------------------------------------------------------------------------
class RelevanceGate(BaseModel):
    """1차 관련성 판정 결과. **NewsOntology 와 의도적으로 분리한 모델이다.**

    GeekNews 같은 종합 소스는 제목만으로 AI 관련성을 알 수 없고, 최근 50건 중
    약 40%가 비-AI 항목이다(결정 로그 D-014). 이 항목들을 그대로 정밀 추출
    (Opus 5 / effort high)로 넘기면 버려질 결과에 비용을 쓰게 된다. 그래서 저비용
    모델로 통과 여부만 먼저 판정한다. (결정 로그 D-015)

    온톨로지 필드를 하나도 포함하지 않는 이유: 게이트는 "이 기사를 분석할
    가치가 있는가"만 답한다. 여기서 분류까지 시키면 저비용 모델의 분류 결과가
    정밀 추출 결과와 섞여 어느 쪽 품질을 재는지 알 수 없게 된다.

    판정 기준은 `schema.md` 의 "수집 대상 범위" 절과 `prompts/relevance_gate.v1.md`
    가 함께 정의한다.
    """

    model_config = ConfigDict(populate_by_name=True)

    is_relevant: bool = Field(
        alias="관련있음",
        description="AI 엔지니어링 또는 업무자동화와 관련이 있으면 true",
    )
    reason: str = Field(
        alias="근거",
        description="판정 근거 한 줄.",
        # [주의] min_length 는 공백을 포함한 전체 문자 수를 센다.
        # 이 값은 **품질 보장이 아니다.** 언어에 따라 같은 글자 수의 정보량이
        # 크게 다르고(한국어 10자 ≒ 영어 30자), 무의미한 문자열도 길이만
        # 넘기면 통과한다. 빈 문자열과 "네"/"없음" 같은 무응답만 막는 하한선이다.
        # 근거의 실제 품질은 사람이 스킵 로그를 표본 검토해 판단한다.
        # (Impact.rationale 에서 "공백 제외"로 잘못 적었던 것을 같은 커밋에서 정정)
        min_length=10,
        max_length=200,
    )
