"""LLM 프로바이더 추상화 + Anthropic 구현체.

기본 구현체는 Anthropic(`claude-opus-5`)이지만, 호출부가 특정 SDK에 묶이지
않도록 얇은 Protocol 을 둔다. 이후 eval/ 의 judge 도 같은 인터페이스를 쓴다.
(README 결정 로그 D-004)

이 파이프라인의 LLM 호출은 전부 "구조화된 결과를 받는" 형태다. 자유 텍스트를
받아 우리가 파싱하는 경로는 두지 않는다 — 파싱 실패와 스키마 실패를 구분할 수
없게 되기 때문. 그래서 Protocol 의 유일한 메서드가 `parse_into` 다.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Generic, Protocol, TypeVar, runtime_checkable

import anthropic
from dotenv import load_dotenv
from pydantic import BaseModel, ValidationError

from extraction.egress import (
    assert_internal_endpoint,
    assert_vendor_call_allowed,
    endpoint_from_env,
)

T = TypeVar("T", bound=BaseModel)

PROJECT_ROOT = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# 예외
# ---------------------------------------------------------------------------
class LLMError(Exception):
    """LLM 레이어의 기반 예외."""


class SchemaMismatchError(LLMError):
    """재시도 후에도 요구한 스키마로 파싱되지 않았다.

    호출부는 이 예외를 "이 기사는 격리한다"는 신호로 해석해야 한다.
    부분적으로 채워진 결과를 통과시키면 Vault 가 조용히 오염된다.
    """

    def __init__(self, message: str, *, attempts: int, last_error: str | None = None) -> None:
        super().__init__(message)
        self.attempts = attempts
        self.last_error = last_error


# ---------------------------------------------------------------------------
# 결과 타입
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Usage:
    """프로바이더 중립 사용량. observability 단계에서 집계할 최소 단위.

    `thinking_tokens` 는 **`output_tokens` 에 포함된 부분집합**이다(별도 과금이
    아니라 출력 단가로 함께 청구된다). 비용을 계산할 때 두 값을 더하면 안 된다.
    """

    input_tokens: int | None = None
    output_tokens: int | None = None
    thinking_tokens: int | None = None
    cache_read_input_tokens: int | None = None
    cache_creation_input_tokens: int | None = None
    latency_s: float | None = None
    model: str | None = None


@dataclass(frozen=True)
class StructuredResult(Generic[T]):
    """검증을 통과한 결과 + 사용량."""

    value: T
    usage: Usage
    attempts: int = 1


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------
@runtime_checkable
class LLMClient(Protocol):
    """모든 LLM 프로바이더가 만족해야 하는 최소 계약."""

    def parse_into(
        self,
        *,
        system: str,
        user: str,
        output_model: type[T],
    ) -> StructuredResult[T]:
        """구조화된 결과를 받아 `output_model` 로 검증해 돌려준다.

        스키마 검증에 최종 실패하면 `SchemaMismatchError` 를 올린다.
        """
        ...


# ---------------------------------------------------------------------------
# Anthropic 구현
# ---------------------------------------------------------------------------
def load_env(project_root: Path = PROJECT_ROOT) -> None:
    """프로젝트 루트의 .env 를 읽는다. 이미 설정된 환경변수는 덮어쓰지 않는다."""
    load_dotenv(project_root / ".env", override=False)


class AnthropicClient:
    """Anthropic Messages API 기반 구조화 추출 클라이언트.

    ## 구조화 출력 방식: `messages.parse(output_format=...)` 를 고른 이유

    후보는 두 가지였다.

    1. **strict tool use** — 도구 하나를 정의하고 `strict: True` 로 입력 스키마를
       강제한 뒤 `tool_use` 블록의 `input` 을 꺼내 쓴다.
    2. **structured outputs** — `output_config.format` 으로 응답 자체를 JSON
       스키마에 묶는다. SDK 의 `messages.parse(output_format=<pydantic 모델>)` 이
       이걸 감싸고 검증된 인스턴스(`parsed_output`)까지 돌려준다.

    2를 골랐다. 이유:
      - 우리가 하는 일은 **도구 호출이 아니라 단일 추출**이다. 1은 "모델이 도구를
        부르도록 유도"하는 우회로이고, 도구를 안 부르고 텍스트로 답하는 경로가
        항상 남는다. 그 분기를 호출부가 방어해야 한다.
      - 2는 pydantic 모델을 그대로 넘기고 검증된 객체를 그대로 받는다. 스키마
        정의(schema.py)와 런타임 검증 사이에 손으로 옮겨 적는 단계가 없다 —
        SSoT 를 하나로 유지하려는 이 프로젝트의 전제(D-002)와 맞는다.
      - 1은 도구가 여러 개이거나 모델이 호출 여부를 스스로 판단해야 할 때 값을
        하는데, 여기엔 그런 요구가 없다.
    (README 결정 로그 D-009)

    ## 실패 처리
      - 스키마 불일치: 검증 오류 메시지를 프롬프트에 되먹여 **1회 재시도**.
        그래도 실패하면 `SchemaMismatchError` 를 올린다 (부분 결과를 반환하지 않는다).
      - API 오류: 연결 오류/429/5xx 는 SDK 가 자체 백오프로 재시도한다
        (`max_retries`). 그 뒤에도 실패하면 `anthropic` 예외를 그대로 올린다 —
        400 같은 비재시도 오류를 여기서 삼키면 원인이 가려진다.
    """

    def __init__(
        self,
        *,
        model: str = "claude-opus-5",
        max_tokens: int = 8000,
        effort: str | None = "high",
        thinking: bool = True,
        temperature: float | None = None,
        max_retries: int = 2,
        timeout: float = 120.0,
        client: anthropic.Anthropic | None = None,
        stage: str = "unspecified",
    ) -> None:
        # --- 외부 벤더 호출 게이트 (ADR-017) ---------------------------------
        # **객체 생성 시점**에 막는다. 호출 시점이 아니라 여기인 이유는, 클라이언트가
        # 만들어진 뒤에는 어디서든 부를 수 있어 차단 지점이 흩어지기 때문이다.
        # Protocol 구현체가 태어나는 자리가 이 레포에서 벤더로 나가는 유일한 문이다.
        #
        # 목 클라이언트 주입은 통과시킨다 — 네트워크로 나가지 않는다. 다만 **진짜
        # SDK 객체를 주입하는 경로는 통과시키지 않는다.** 그 경로까지 열어 두면
        # `client=` 한 글자가 게이트 전체의 우회로가 된다.
        self.stage = stage
        if client is None or isinstance(client, anthropic.Anthropic):
            assert_vendor_call_allowed(stage=stage, model=model)
            assert_internal_endpoint(endpoint_from_env(), field="LLM 엔드포인트 override")

        self.model = model
        self.max_tokens = max_tokens
        # effort/thinking/temperature 는 모델마다 지원 범위가 다르다. 지원하지 않는
        # 모델에는 None / False 를 넘겨 파라미터 자체를 빼야 한다.
        #   - Haiku 4.5: output_config.effort 거부(400). temperature 는 수용.
        #   - Opus 5   : temperature 거부(400, "deprecated for this model").
        # (결정 로그 D-032)
        self.effort = effort
        self.thinking = thinking
        self.temperature = temperature

        if client is not None:
            # 테스트에서 목 클라이언트를 주입하는 경로.
            self._client = client
            return

        load_env()
        api_key = os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("LLM_API_KEY")
        if not api_key:
            raise LLMError(
                "ANTHROPIC_API_KEY 가 없습니다. .env.example 을 .env 로 복사해 채우세요."
            )

        # identity-linked 키는 어느 워크스페이스에서 동작하는지를 헤더로 요구한다
        # (없으면 400 "anthropic-workspace-id is required"). 일반 키에는 불필요하므로
        # 값이 있을 때만 붙인다.
        headers: dict[str, str] = {}
        workspace_id = os.environ.get("ANTHROPIC_WORKSPACE_ID")
        if workspace_id:
            headers["anthropic-workspace-id"] = workspace_id

        self._client = anthropic.Anthropic(
            api_key=api_key,
            max_retries=max_retries,
            timeout=timeout,
            default_headers=headers or None,
        )

    @classmethod
    def from_config(
        cls,
        config: dict[str, Any],
        stage: str = "extraction",
        **overrides: Any,
    ) -> "AnthropicClient":
        """config.yaml 의 `llm.<stage>` 블록으로 클라이언트를 만든다.

        단계마다 모델이 다르다(결정 로그 D-015). `stage` 하위 블록이 없으면
        `llm:` 최상위를 그대로 읽어 이전 단일 모델 설정과도 호환된다.

        Args:
            stage: `relevance_gate` | `extraction`
        """
        llm = config.get("llm") or {}
        section = llm.get(stage)
        if not isinstance(section, dict):
            section = llm  # 하위 블록이 없는 구버전 설정

        kwargs: dict[str, Any] = {
            "model": section.get("model", "claude-opus-5"),
            "max_tokens": section.get("max_tokens", 8000),
            # effort 가 명시적으로 null 이면 파라미터를 보내지 않는다.
            "effort": section.get("effort", "high"),
            "thinking": section.get("thinking", "adaptive") != "disabled",
            # temperature 는 기본이 None(= 보내지 않음)이다. 지원하는 모델에만
            # 설정에서 명시적으로 켠다.
            "temperature": section.get("temperature"),
            # 어느 호출 지점인지를 그대로 넘긴다. 승인 없이 파이프라인을 돌렸을 때
            # 예외 메시지가 단계 이름을 말해 주는 근거다 (ADR-017).
            "stage": stage,
        }
        kwargs.update(overrides)
        return cls(**kwargs)

    # -- 내부 -------------------------------------------------------------
    def _request_kwargs(self) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "model": self.model,
            "max_tokens": self.max_tokens,
        }
        if self.effort:
            kwargs["output_config"] = {"effort": self.effort}
        if self.thinking:
            # 통제어휘 선택과 영향도 판정은 판단이 섞이는 작업이라 기본으로 켠다.
            kwargs["thinking"] = {"type": "adaptive"}
        if self.temperature is not None:
            # SDK 1.x 의 messages.create/parse 에는 `temperature` 명명 인자가 없다.
            # 최신 모델에서 제거된 파라미터라서인데, Haiku 4.5 같은 구세대 모델은
            # 여전히 받는다. 그래서 extra_body 로 실어 보낸다. (D-032)
            kwargs["extra_body"] = {"temperature": self.temperature}
        return kwargs

    def _usage(self, response: Any, latency_s: float | None = None) -> Usage:
        usage = getattr(response, "usage", None)
        details = getattr(usage, "output_tokens_details", None)
        return Usage(
            input_tokens=getattr(usage, "input_tokens", None),
            output_tokens=getattr(usage, "output_tokens", None),
            # thinking 토큰은 output_tokens 안에 이미 포함돼 있다. 분리해 보는 건
            # "얼마나 생각했나"를 관측하기 위해서지 따로 더하기 위해서가 아니다.
            thinking_tokens=getattr(details, "thinking_tokens", None),
            cache_read_input_tokens=getattr(usage, "cache_read_input_tokens", None),
            cache_creation_input_tokens=getattr(usage, "cache_creation_input_tokens", None),
            latency_s=latency_s,
            model=getattr(response, "model", self.model),
        )

    @staticmethod
    def _first_text(response: Any) -> str:
        for block in getattr(response, "content", None) or []:
            if getattr(block, "type", None) == "text":
                return getattr(block, "text", "") or ""
        return ""

    @staticmethod
    def _validation_message(raw_text: str, output_model: type[T]) -> str:
        """실패 원인을 사람이 읽을 수 있는 문장으로 만든다 (재시도 프롬프트에 넣는다)."""
        if not raw_text:
            return "응답에 텍스트 블록이 없습니다."
        try:
            output_model.model_validate_json(raw_text)
        except ValidationError as exc:
            return str(exc)
        except Exception as exc:  # JSON 자체가 깨진 경우
            return f"{type(exc).__name__}: {exc}"
        return "알 수 없는 이유로 parsed_output 이 비어 있습니다."

    # -- 공개 API ---------------------------------------------------------
    def parse_into(
        self,
        *,
        system: str,
        user: str,
        output_model: type[T],
    ) -> StructuredResult[T]:
        """구조화 출력으로 `output_model` 인스턴스를 받는다. 실패 시 1회 재시도."""
        messages: list[dict[str, Any]] = [{"role": "user", "content": user}]
        last_error: str | None = None

        for attempt in (1, 2):
            started = time.monotonic()
            response = self._client.messages.parse(
                system=system,
                messages=messages,
                output_format=output_model,
                **self._request_kwargs(),
            )
            latency_s = time.monotonic() - started

            parsed = getattr(response, "parsed_output", None)
            if isinstance(parsed, output_model):
                return StructuredResult(
                    value=parsed,
                    usage=self._usage(response, latency_s=latency_s),
                    attempts=attempt,
                )

            # parsed_output 이 비었다는 건 스키마 강제가 뚫렸다는 뜻이다.
            # 원문 텍스트를 다시 검증해 정확한 오류 메시지를 얻는다.
            raw_text = self._first_text(response)
            last_error = self._validation_message(raw_text, output_model)

            if attempt == 1:
                messages = [
                    {"role": "user", "content": user},
                    {"role": "assistant", "content": raw_text or "(빈 응답)"},
                    {
                        "role": "user",
                        "content": (
                            "이전 응답이 요구한 스키마를 만족하지 않는다. 아래 오류를 고쳐 "
                            "동일한 스키마로 다시 출력하라. 설명 없이 결과만 낸다.\n\n"
                            f"오류:\n{last_error}"
                        ),
                    },
                ]

        raise SchemaMismatchError(
            f"{output_model.__name__} 스키마 검증에 2회 실패했습니다.",
            attempts=2,
            last_error=last_error,
        )


# ---------------------------------------------------------------------------
# 프로바이더 선택 (ADR-018)
# ---------------------------------------------------------------------------
#: `config.yaml: llm.provider` 가 고를 수 있는 값.
PROVIDERS = ("vllm", "anthropic")

#: 내부 vLLM 에서 의미가 없는 벤더 전용 파라미터. 조용히 버리지 않고 **이름을
#: 남긴다** — 파라미터가 사라진 것과 무시된 것은 다르고, 대조 실행에서 "무엇이
#: 달랐나"를 적으려면 그 목록이 필요하다 (사전 등록 §2.1).
VENDOR_ONLY_PARAMS = ("effort", "thinking")


def client_from_config(
    config: dict[str, Any],
    stage: str = "extraction",
    **overrides: Any,
) -> LLMClient:
    """`llm.provider` 를 보고 단계용 클라이언트를 만든다.

    호출부(extractor / export.runner / eval.runner)가 구현체 이름을 알지 않게
    하는 것이 목적이다. **이전 범위가 "단계 3개"가 아니라 "생성 지점 5개"였던
    이유가 이것이다** — 생성이 호출부마다 흩어져 있으면 프로바이더를 바꿀 때
    한 줄이 조용히 남는다 (ADR-017 Evidence, session-02 §3.1).

    Args:
        stage: `relevance_gate` | `extraction` | `eval_judge`.
    """
    llm = (config or {}).get("llm") or {}
    provider = (llm.get("provider") or "vllm").strip().lower()
    if provider not in PROVIDERS:
        raise LLMError(
            f"알 수 없는 llm.provider: {provider!r}. 가능한 값: {', '.join(PROVIDERS)}"
        )

    if provider == "anthropic":
        return AnthropicClient.from_config(config, stage=stage, **overrides)

    from extraction.vllm import VLLMClient

    section = llm.get(stage)
    if not isinstance(section, dict):
        section = llm
    vllm_cfg = llm.get("vllm") or {}

    kwargs: dict[str, Any] = {
        "model": section.get("model"),
        "max_tokens": section.get("max_tokens", 8000),
        # 벤더 쪽 추출 설정은 temperature 를 보내지 못했다 — Opus 5 가 거부한다
        # (D-033). vLLM 은 받으므로 **게이트와 같은 이유로**(D-032: 같은 입력이
        # 실행마다 다르게 판정되면 안 된다) 0 을 기본으로 둔다. 이것은 모델 교체와
        # 함께 바뀌는 파라미터 차이이고, 대조 결과에 그대로 적는다.
        "temperature": section.get("temperature", 0.0),
        "system_as_user": bool(vllm_cfg.get("system_as_user", False)),
        "stage": stage,
    }
    if vllm_cfg.get("base_url"):
        kwargs["base_url"] = vllm_cfg["base_url"]
    kwargs.update(overrides)
    return VLLMClient(**kwargs)
