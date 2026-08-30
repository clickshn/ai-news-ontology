"""RawItem 1건 -> NewsOntology 변환.

수집 → 추출 수직 슬라이스의 마지막 단계. 프롬프트를 조립하고, LLM 을 호출하고,
결과를 `NewsOntology` 로 검증해 돌려준다.

이 모듈이 지키는 것:
  - 프롬프트는 코드에 하드코딩하지 않는다. `extraction/prompts/*.vN.md` 에서
    읽고, 어떤 버전을 썼는지 결과에 함께 실어 보낸다. (결정 로그 D-010)
  - 통제어휘는 프롬프트에 하드코딩하지 않고 schema.py Enum 에서 주입한다.
    (결정 로그 D-012)
  - 검증에 실패하면 부분 결과를 반환하지 않는다. 예외를 올려 호출부가 격리하게 한다.

CLI 확인용:
    python -m extraction.extractor --source "arXiv cs.CL (Atom API)" --index 0
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from collectors.base import RawItem
from collectors.rss import collect, load_config
from extraction.llm import AnthropicClient, LLMClient, SchemaMismatchError, StructuredResult, Usage
from extraction.schema import NewsOntology, ReleaseType, RelevanceGate, TechDomain
from observability.events import (
    InMemoryObserver,
    MultiObserver,
    NullObserver,
    PipelineObserver,
    SkipRecord,
    UnknownCompanyRecord,
    observer_from_config,
)

PROMPT_DIR = Path(__file__).resolve().parent / "prompts"
DEFAULT_PROMPT = "extract_ontology.v3.md"
GATE_PROMPT = "relevance_gate.v1.md"

_SYSTEM_RE = re.compile(r"^#\s*System\s*$", re.MULTILINE)
_USER_RE = re.compile(r"^#\s*User\s*$", re.MULTILINE)
_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)


# ---------------------------------------------------------------------------
# 프롬프트 로딩
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Prompt:
    """`# System` / `# User` 로 나뉜 프롬프트 템플릿."""

    name: str
    system: str
    user: str

    def render(self, **variables: Any) -> tuple[str, str]:
        """`{{name}}` 치환. 값이 없는 변수는 '(정보 없음)' 으로 채운다.

        치환되지 않은 자리를 남기면 모델이 그 리터럴을 데이터로 읽는다.
        """

        def substitute(text: str) -> str:
            def repl(match: re.Match[str]) -> str:
                key = match.group(1).strip()
                value = variables.get(key)
                return "(정보 없음)" if value in (None, "") else str(value)

            return re.sub(r"\{\{([^}]+)\}\}", repl, text)

        return substitute(self.system), substitute(self.user)


def load_prompt(filename: str = DEFAULT_PROMPT, *, prompt_dir: Path = PROMPT_DIR) -> Prompt:
    """프롬프트 파일을 읽어 System/User 로 자른다."""
    path = prompt_dir / filename
    text = _COMMENT_RE.sub("", path.read_text(encoding="utf-8"))

    sys_match = _SYSTEM_RE.search(text)
    user_match = _USER_RE.search(text)
    if not sys_match or not user_match:
        raise ValueError(f"{filename}: '# System' 과 '# User' 섹션이 모두 필요합니다")
    if user_match.start() < sys_match.start():
        raise ValueError(f"{filename}: '# System' 이 '# User' 보다 앞에 와야 합니다")

    system = text[sys_match.end() : user_match.start()].strip()
    user = text[user_match.end() :].strip()
    return Prompt(name=filename, system=system, user=user)


# ---------------------------------------------------------------------------
# 어휘 주입
# ---------------------------------------------------------------------------
def _vocab_block(enum_cls: type) -> str:
    """Enum 을 프롬프트에 넣을 불릿 목록으로 만든다."""
    return "\n".join(f"- `{member.value}`" for member in enum_cls)


def build_variables(item: RawItem) -> dict[str, Any]:
    """프롬프트 치환 변수. 어휘 목록은 schema.py Enum 이 유일한 출처다."""
    return {
        "tech_domain_list": _vocab_block(TechDomain),
        "release_type_list": _vocab_block(ReleaseType),
        "source_name": item.source_name,
        "published_at": item.published_at.isoformat() if item.published_at else None,
        "url": str(item.url),
        "title": item.title,
        "body": item.body,
    }


# ---------------------------------------------------------------------------
# 추출
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class ExtractionResult:
    """추출 1건의 결과 + 재현에 필요한 메타데이터."""

    item: RawItem
    ontology: NewsOntology
    prompt_name: str
    usage: Usage
    attempts: int


def extract_ontology(
    item: RawItem,
    client: LLMClient,
    *,
    prompt: Prompt | None = None,
) -> ExtractionResult:
    """RawItem 1건을 NewsOntology 로 구조화한다.

    Raises:
        SchemaMismatchError: 재시도 후에도 스키마 검증에 실패한 경우.
        anthropic.APIError: SDK 자체 재시도 후에도 남은 API 오류.
    """
    prompt = prompt or load_prompt()
    system, user = prompt.render(**build_variables(item))

    result: StructuredResult[NewsOntology] = client.parse_into(
        system=system, user=user, output_model=NewsOntology
    )
    return ExtractionResult(
        item=item,
        ontology=result.value,
        prompt_name=prompt.name,
        usage=result.usage,
        attempts=result.attempts,
    )


def record_unknown_companies(
    ontology: NewsOntology,
    item: RawItem,
    observer: PipelineObserver,
) -> None:
    """정규화에 실패한 기업 표기를 사전 보강 큐로 흘린다 (D-035).

    `CompanyRef` validator 가 아니라 **여기서** 기록하는 이유는 두 가지다.

    1. 레코드에 필요한 `source_article` 을 validator 는 볼 수 없다. `CompanyRef`
       는 자기가 어느 기사에서 나왔는지 모르고, 알게 하려면 스키마에 기사
       정보를 끌고 들어와야 한다.
    2. validator 는 테스트·골든셋 로딩·재검증에서도 돈다. 거기서 남긴 줄은
       실제 파이프라인 실행에서 나온 게 아니라 `occurrence_count` 를 오염시킨다.
       "몇 번 나왔는가"가 사전 보강 판단의 근거이므로 이 오염은 치명적이다.

    즉 정규화 **판정**은 스키마 층(항상, 결정적으로)이고, 정규화 실패의
    **기록**은 파이프라인 층(실제 실행에서만)이다.
    """
    for company in ontology.companies:
        if company.resolved:
            continue
        observer.record_unknown_company(
            UnknownCompanyRecord(raw_name=company.raw, source_article=str(item.url))
        )


# ---------------------------------------------------------------------------
# 1단계: 관련성 게이트
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class RelevanceResult:
    """게이트 판정 1건 + 재현에 필요한 메타데이터."""

    item: RawItem
    gate: RelevanceGate
    prompt_name: str
    usage: Usage

    @property
    def is_relevant(self) -> bool:
        return self.gate.is_relevant


def check_relevance(
    item: RawItem,
    client: LLMClient,
    *,
    prompt: Prompt | None = None,
) -> RelevanceResult:
    """저비용 모델로 "정밀 추출에 넘길 가치가 있는가"만 판정한다.

    분류·요약은 하지 않는다. 판정 기준은 schema.md 의 "수집 대상 범위" 절.
    """
    prompt = prompt or load_prompt(GATE_PROMPT)
    system, user = prompt.render(**build_variables(item))

    result: StructuredResult[RelevanceGate] = client.parse_into(
        system=system, user=user, output_model=RelevanceGate
    )
    return RelevanceResult(
        item=item, gate=result.value, prompt_name=prompt.name, usage=result.usage
    )


# ---------------------------------------------------------------------------
# 2단계 파이프라인
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class PipelineResult:
    """게이트 -> (통과 시) 정밀 추출 까지의 결과.

    `extraction` 이 None 이면 게이트에서 걸러진 것이다. 예외로 표현하지 않는
    이유: 스킵은 오류가 아니라 정상적인 결과다.
    """

    item: RawItem
    relevance: RelevanceResult
    extraction: ExtractionResult | None = None

    @property
    def skipped(self) -> bool:
        return self.extraction is None


def process_item(
    item: RawItem,
    *,
    gate_client: LLMClient,
    extraction_client: LLMClient,
    observer: PipelineObserver | None = None,
    gate_prompt: Prompt | None = None,
    extraction_prompt: Prompt | None = None,
) -> PipelineResult:
    """RawItem 1건을 게이트 -> 정밀 추출 순으로 처리한다.

    게이트가 false 를 내면 **정밀 추출 클라이언트를 호출하지 않는다.** 이 구조가
    비용 절감의 전부이므로, 호출되지 않는다는 사실을 테스트로 고정해 둔다
    (`tests/test_relevance_gate.py`).

    스킵된 항목은 observer 로 기록한다. 기본은 no-op 이지만, 무엇을 왜 버렸는지
    남길 자리를 지금 만들어 둬야 나중에 게이트의 오탐을 검증할 수 있다 (D-016).
    """
    observer = observer or NullObserver()

    relevance = check_relevance(item, gate_client, prompt=gate_prompt)
    if not relevance.is_relevant:
        observer.record_skip(
            SkipRecord(
                url=str(item.url),
                title=item.title,
                source_name=item.source_name,
                reason=relevance.gate.reason,
                model=relevance.usage.model,
                prompt_name=relevance.prompt_name,
            )
        )
        return PipelineResult(item=item, relevance=relevance)

    extraction = extract_ontology(item, extraction_client, prompt=extraction_prompt)
    record_unknown_companies(extraction.ontology, item, observer)
    return PipelineResult(item=item, relevance=relevance, extraction=extraction)


# ---------------------------------------------------------------------------
# CLI (수집 -> 추출 슬라이스 확인용)
# ---------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="RSS 1건을 수집해 관련성 게이트 -> NewsOntology 추출까지 실행한다"
    )
    parser.add_argument("--source", default=None, help="config.yaml 의 소스 이름")
    parser.add_argument("--index", type=int, default=0, help="해당 소스에서 몇 번째 기사인지")
    parser.add_argument("--prompt", default=DEFAULT_PROMPT, help="추출 프롬프트 파일명")
    parser.add_argument("--gate-prompt", default=GATE_PROMPT, help="게이트 프롬프트 파일명")
    parser.add_argument(
        "--gate-only",
        action="store_true",
        help="관련성 게이트만 실행하고 정밀 추출(고비용)은 건너뛴다",
    )
    args = parser.parse_args(argv)

    config = load_config()
    items = list(collect(config, source_name=args.source, limit_per_source=args.index + 1))
    if not items:
        print("수집된 기사가 없습니다.", file=sys.stderr)
        return 1
    item = items[min(args.index, len(items) - 1)]

    print("=" * 72)
    print(f"소스   : {item.source_name}")
    print(f"제목   : {item.title}")
    print(f"URL    : {item.url}")
    print(f"발행일 : {item.published_at}")
    print(f"본문   : {len(item.body)}자")
    print("=" * 72)

    # 화면 출력용(메모리) + 파일 기록용(config 에 따라 JSONL 또는 no-op).
    # 관측이 꺼져 있으면 두 번째가 NullObserver 라 CLI 동작은 그대로다 (D-008).
    memory = InMemoryObserver()
    observer = MultiObserver(memory, observer_from_config(config))
    gate_client = AnthropicClient.from_config(config, stage="relevance_gate")

    # --gate-only 는 정밀 추출 클라이언트를 아예 만들지 않는다.
    if args.gate_only:
        relevance = check_relevance(item, gate_client, prompt=load_prompt(args.gate_prompt))
        verdict = "관련 있음" if relevance.is_relevant else "관련 없음 (스킵)"
        print(f"[게이트] {verdict}")
        print(f"  근거: {relevance.gate.reason}")
        print(
            f"  model={relevance.usage.model} prompt={relevance.prompt_name} "
            f"in={relevance.usage.input_tokens} out={relevance.usage.output_tokens}",
            file=sys.stderr,
        )
        return 0

    extraction_client = AnthropicClient.from_config(config, stage="extraction")
    try:
        result = process_item(
            item,
            gate_client=gate_client,
            extraction_client=extraction_client,
            observer=observer,
            gate_prompt=load_prompt(args.gate_prompt),
            extraction_prompt=load_prompt(args.prompt),
        )
    except SchemaMismatchError as exc:
        print(f"[격리] 스키마 검증 실패 ({exc.attempts}회): {exc.last_error}", file=sys.stderr)
        return 2

    print(f"[게이트] {'통과' if result.relevance.is_relevant else '스킵'}")
    print(f"  근거: {result.relevance.gate.reason}")

    if result.skipped:
        print("-" * 72)
        print("정밀 추출을 건너뛰었습니다. 스킵 기록:")
        for record in memory.skips:
            print(json.dumps(record.to_dict(), ensure_ascii=False, indent=2))
        return 0

    print("-" * 72)
    extraction = result.extraction
    assert extraction is not None  # skipped 가 False 이면 항상 존재한다
    print(
        json.dumps(
            extraction.ontology.model_dump(by_alias=True, mode="json"),
            ensure_ascii=False,
            indent=2,
        )
    )
    if memory.unknown_companies:
        print("-" * 72)
        print("미등록 기업 (company_aliases 보강 후보):")
        for record in memory.unknown_companies:
            print(f"  - {record.raw_name}")

    print("-" * 72)
    print(
        f"gate: model={result.relevance.usage.model} "
        f"in={result.relevance.usage.input_tokens} out={result.relevance.usage.output_tokens}\n"
        f"extract: prompt={extraction.prompt_name} attempts={extraction.attempts} "
        f"model={extraction.usage.model} in={extraction.usage.input_tokens} "
        f"out={extraction.usage.output_tokens}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
