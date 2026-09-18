"""내부 vLLM(OpenAI 호환) 구조화 추출 클라이언트 — `LLMClient` 두 번째 구현체.

## 왜 있나

이 레포는 `multiagent-research-lab`(MARA)의 코퍼스를 만드는 생산자이고, MARA 의
전제는 **외부 LLM 벤더 비의존**이다 (MARA ADR-021, 이쪽 ADR-017). 그런데 이 레포의
세 단계(관련성 게이트 / 추출 / eval judge)가 전부 벤더를 가리키고 있었다. ADR-017 의
게이트는 그 상태를 **유예**했을 뿐 없애지 못한다 — 경로를 없애는 것은 이 모듈이다.

목적지는 `.env` 의 `VLLM_BASE` 하나뿐이다 (KT Cloud AI Nexus, MARA ADR-003).

## 왜 `openai` SDK 를 쓰지 않았나

OpenAI 호환 엔드포인트를 부르는 가장 흔한 방법은 `openai` 패키지지만 쓰지 않았다.

1. **그 SDK 의 기본 `base_url` 이 벤더다.** 설치하는 순간 "설정 한 줄로 벤더에
   닿는 문"이 하나 더 생긴다. 이 레포가 방금 `anthropic` 으로 겪은 문제를 똑같이
   한 번 더 만드는 셈이다.
2. **게이트가 그 SDK 를 구분하지 못한다.** `.claude/hooks/check-external-llm.sh` 는
   `import openai` 와 `OpenAI(` 를 **차단 대상**으로 들고 있다 (VENDOR_SDK / VENDOR_CALL).
   정상 사용과 위반이 같은 문자열이 되면 `anthropic` 에서 겪은 정탐·오탐 충돌
   (D-061)이 재발하고, 그때는 훅에서 패턴을 빼는 수밖에 없어진다.
3. 필요한 것은 `POST /chat/completions` 하나다. 표준 라이브러리로 충분하다.

그래서 **새 의존성 0개**로 `urllib.request` 위에 얇게 얹었다 (D-063).

## structured output — 벤더 SDK 와 같은 것이 아니다

`AnthropicClient` 는 `messages.parse(output_format=<pydantic 모델>)` 로 **SDK 가
스키마 강제와 검증까지** 해 준다 (ADR-003). vLLM 에는 그 계층이 없다. 여기서는

  1. `output_model.model_json_schema(by_alias=True)` 로 JSON Schema 를 만들고
  2. `response_format={"type": "json_schema", ...}` 로 디코딩을 제약하고
  3. 돌아온 문자열을 **우리가** `model_validate_json` 으로 검증한다.

2번의 강제는 **문법(grammar) 수준**이다. 타입·필수 키·enum 값은 디코딩 단계에서
막히지만, `minLength` / `maxLength` / `minItems` 같은 **수량 제약은 백엔드에 따라
강제되지 않을 수 있다.** 그 몫은 3번의 pydantic 검증이 잡고, 잡히면 재시도 →
`SchemaMismatchError` 로 같은 경로를 탄다. **이 차이는 결함이 아니라 측정 대상이다**
(사전 등록 §2.1-2: "강제 수준이 다르면 수치가 모델 능력이 아니라 디코딩 설정을 잰다").
실측 결과는 ADR-018 에 있다.

## 재시도 정책은 벤더 쪽과 같게 맞췄다

사전 등록 §2.1-3 이 요구한 조건이다. 스키마 실패 시 **오류를 되먹여 1회 재시도**,
그래도 실패하면 `SchemaMismatchError(attempts=2)`. 횟수가 다르면 실패율을 비교할 수
없다. HTTP 계층 재시도(429/5xx)는 그와 별개이며 스키마 시도 횟수에 넣지 않는다.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

from extraction.egress import assert_internal_endpoint
from extraction.llm import LLMError, SchemaMismatchError, StructuredResult, Usage, load_env

T = TypeVar("T", bound=BaseModel)

PROJECT_ROOT = Path(__file__).resolve().parent.parent

#: 엔드포인트를 담는 환경변수. `endpoint_from_env()` 와 같은 이름을 본다 —
#: 검사하는 경로와 실제로 쓰이는 경로가 갈라지면 검사가 의미를 잃는다.
BASE_URL_ENV = "VLLM_BASE"
MODEL_ENV = "VLLM_MODEL"

#: HTTP 재시도 대상. 400 은 넣지 않는다 — 요청이 틀린 것을 재시도로 덮으면
#: 원인이 가려진다 (`AnthropicClient` 의 같은 판단).
RETRYABLE_STATUS = frozenset({408, 409, 429, 500, 502, 503, 504})


class VLLMEndpointError(LLMError):
    """엔드포인트 설정이 없거나 응답이 OpenAI 호환 형식이 아니다."""


def base_url_from_env(*, project_root: Path | None = None) -> str:
    """`.env` 의 `VLLM_BASE`. 없으면 진행하지 않는다.

    기본값을 코드에 두지 않는다. 엔드포인트 URL 자체가 자격증명이고(MARA ADR-010),
    기본값이 있으면 설정 누락이 조용히 다른 목적지로 흘러갈 수 있다.

    `PROJECT_ROOT` 를 기본 인자로 굳히지 않고 호출 시점에 읽는다 — 기본 인자는
    import 시점에 평가돼서, 다른 루트를 가리켜도 실제로는 레포의 `.env` 를 읽는다.
    """
    load_env(project_root or PROJECT_ROOT)
    base = (os.environ.get(BASE_URL_ENV) or "").strip().rstrip("/")
    if not base:
        raise VLLMEndpointError(
            f"{BASE_URL_ENV} 가 없습니다. .env.example 을 .env 로 복사해 채우세요. "
            "이 레포의 LLM 목적지는 내부 vLLM 하나뿐입니다 (ADR-018)."
        )
    return base


class VLLMClient:
    """OpenAI 호환 `/chat/completions` 로 구조화 결과를 받는다.

    `AnthropicClient` 와 같은 `LLMClient` Protocol 을 만족하므로 호출부
    (`extract_ontology` / `check_relevance` / `judge_summary`)는 바뀌지 않는다.
    프로바이더 교체 지점을 한 곳으로 모으려고 Protocol 을 둔 것이 D-004 였고,
    이 모듈이 그 설계가 실제로 값을 하는 첫 사례다.
    """

    def __init__(
        self,
        *,
        model: str | None = None,
        max_tokens: int = 8000,
        temperature: float | None = 0.0,
        base_url: str | None = None,
        max_retries: int = 2,
        timeout: float = 300.0,
        stage: str = "unspecified",
        system_as_user: bool = False,
        transport: Any | None = None,
        project_root: Path | None = None,
    ) -> None:
        """
        Args:
            system_as_user: system 역할을 별도 메시지로 보내지 않고 user 메시지
                앞에 붙인다. **프롬프트 파일은 바뀌지 않지만 전달 형태가 바뀐다** —
                gemma 계열 채팅 템플릿이 system 역할을 거부하는 경우에만 켠다.
                실측 결과는 ADR-018 Evidence 에 있다.
            transport: 테스트용 주입 지점. `(url, payload, timeout) -> dict`.
                네트워크로 나가지 않으므로 엔드포인트 검사를 요구하지 않는다.
        """
        self.stage = stage
        self.max_tokens = max_tokens
        self.temperature = temperature
        self.max_retries = max_retries
        self.timeout = timeout
        self.system_as_user = system_as_user
        self._transport = transport
        #: 마지막 `parse_into` 가 받은 **원본 응답들**. 변환하기 전에 디스크로
        #: 옮길 수 있게 남겨 둔다 (D-052: 값비싼 결과와 검증되지 않은 변환 코드를
        #: 같은 트랜잭션에 두지 않는다). 재시도가 있으면 2건이 된다.
        self.last_raw_responses: list[dict[str, Any]] = []

        if transport is not None:
            # 목 주입 경로. 실제 목적지가 없으므로 모델명만 확정한다.
            self.base_url = (base_url or "http://transport.invalid/v1").rstrip("/")
            self.model = model or os.environ.get(MODEL_ENV) or "gemma-4-31B-it"
            return

        self.base_url = (base_url or base_url_from_env(project_root=project_root)).rstrip("/")
        # 목적지가 벤더로 돌려져 있으면 여기서 멈춘다. vLLM 이전 뒤에는 이 검사가
        # 게이트의 실질적인 무게를 받는 자리다 (egress.py 모듈 설명).
        assert_internal_endpoint(self.base_url, field=BASE_URL_ENV)

        self.model = model or os.environ.get(MODEL_ENV) or "gemma-4-31B-it"

    # -- 내부 -------------------------------------------------------------
    @staticmethod
    def response_format_for(output_model: type[T]) -> dict[str, Any]:
        """pydantic 모델 -> vLLM `response_format`.

        `by_alias=True` 인 이유: 이 레포의 스키마는 한국어 alias 가 계약의 표면이고
        (D-003), 프롬프트도 한국어 키로 예시를 준다. 영어 필드명 스키마를 강제하면
        프롬프트와 스키마가 서로 다른 키를 말하게 된다.
        """
        schema = output_model.model_json_schema(by_alias=True)
        return {
            "type": "json_schema",
            "json_schema": {
                "name": output_model.__name__,
                "schema": schema,
                "strict": True,
            },
        }

    def _messages(self, system: str, user: str) -> list[dict[str, str]]:
        if self.system_as_user:
            return [{"role": "user", "content": f"{system}\n\n---\n\n{user}"}]
        return [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]

    def _payload(self, messages: list[dict[str, str]], output_model: type[T]) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "max_tokens": self.max_tokens,
            "response_format": self.response_format_for(output_model),
        }
        if self.temperature is not None:
            payload["temperature"] = self.temperature
        return payload

    def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        """`POST {base_url}{path}`. 429/5xx 만 백오프로 재시도한다."""
        if self._transport is not None:
            return self._transport(self.base_url + path, payload, self.timeout)

        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        last_error: Exception | None = None

        for attempt in range(self.max_retries + 1):
            request = urllib.request.Request(
                self.base_url + path,
                data=body,
                headers={"Content-Type": "application/json", "Accept": "application/json"},
                method="POST",
            )
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    return json.loads(response.read().decode("utf-8"))
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode("utf-8", "replace")[:500]
                last_error = VLLMEndpointError(f"HTTP {exc.code}: {detail}")
                if exc.code not in RETRYABLE_STATUS:
                    raise last_error from exc
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                last_error = VLLMEndpointError(f"{type(exc).__name__}: {exc}")

            if attempt < self.max_retries:
                time.sleep(2**attempt)

        assert last_error is not None
        raise last_error

    @staticmethod
    def _content(response: dict[str, Any]) -> str:
        try:
            message = response["choices"][0]["message"]
        except (KeyError, IndexError, TypeError) as exc:
            raise VLLMEndpointError(
                f"OpenAI 호환 응답 형식이 아닙니다: {json.dumps(response, ensure_ascii=False)[:300]}"
            ) from exc
        return message.get("content") or ""

    def _usage(self, response: dict[str, Any], latency_s: float) -> Usage:
        """OpenAI 형식 usage -> 프로바이더 중립 `Usage`.

        `thinking_tokens` / `cache_*` 는 **0 이 아니라 None** 이다. vLLM 응답에
        해당 개념이 없는 것을 0 으로 적으면 "생각을 0 토큰 했다"로 읽히지만
        실제로는 잴 수 없는 것이다 (`summarize` 가 표본 없는 지표를 None 으로
        두는 것과 같은 규칙).
        """
        usage = response.get("usage") or {}
        return Usage(
            input_tokens=usage.get("prompt_tokens"),
            output_tokens=usage.get("completion_tokens"),
            thinking_tokens=None,
            cache_read_input_tokens=None,
            cache_creation_input_tokens=None,
            latency_s=latency_s,
            model=response.get("model") or self.model,
        )

    @staticmethod
    def _validation_message(raw_text: str, output_model: type[T]) -> str:
        if not raw_text:
            return "응답에 내용이 없습니다."
        try:
            output_model.model_validate_json(raw_text)
        except ValidationError as exc:
            return str(exc)
        except Exception as exc:
            return f"{type(exc).__name__}: {exc}"
        return "알 수 없는 이유로 검증에 실패했습니다."

    # -- 공개 API ---------------------------------------------------------
    def parse_into(
        self,
        *,
        system: str,
        user: str,
        output_model: type[T],
    ) -> StructuredResult[T]:
        """구조화 출력으로 `output_model` 인스턴스를 받는다. 실패 시 1회 재시도.

        재시도 형태를 `AnthropicClient.parse_into` 와 **같게** 맞췄다 (사전 등록
        §2.1-3). 오류 메시지를 assistant/user 턴으로 되먹이는 것까지 같다.
        """
        messages = self._messages(system, user)
        last_error: str | None = None
        self.last_raw_responses = []

        for attempt in (1, 2):
            started = time.monotonic()
            response = self._post("/chat/completions", self._payload(messages, output_model))
            latency_s = time.monotonic() - started
            self.last_raw_responses.append(response)

            raw_text = self._content(response)
            try:
                value = output_model.model_validate_json(raw_text)
            except Exception:
                last_error = self._validation_message(raw_text, output_model)
            else:
                return StructuredResult(
                    value=value,
                    usage=self._usage(response, latency_s),
                    attempts=attempt,
                )

            if attempt == 1:
                messages = [
                    *self._messages(system, user),
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
