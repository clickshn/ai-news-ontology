"""내부 vLLM 엔드포인트의 **structured output 강제 여부**를 판별한다 (ADR-019).

## 왜 모듈로 고정하나

이 서버는 **모르는 파라미터에 400 을 주지 않는다. 200 을 주고 아무 일도 하지
않는다.** structured output 은 파라미터로 켜는 기능이므로, 이 성질은 "강제가 꺼져
있는데 성공한 것처럼 보이는 상태"를 만든다. 그 상태에서 나온 스키마 실패율 0% 는
모델이 잘한 것이 아니라 **아무것도 재지 않은 것**이다.

측정(대조 실행, eval) 전에 매번 확인해야 하는데(`.claude/rules/vllm-endpoint.md`),
**매번 설계를 다시 짜면 매번 틀릴 수 있다.** session-03 의 1차 probe 가 실제로
그랬다 — "스키마를 어겨 보라"로 짜서 `minLength` 가 문자열을 못 닫게 만들었고,
깨진 JSON 이 "문법이 뚫렸다"로 읽혔다. **원인은 정반대였다.**

## 판별 설계 — "JSON 을 쓰지 마라"

    프롬프트: JSON 을 쓰지 마라. 중괄호도 쓰지 마라. 한국어 산문 두 문장.
    스키마  : {"답": enum["예", "아니오"]} required

| 결과 | 뜻 |
|---|---|
| **산문이 나온다** | 강제가 **아니다.** 모델이 지시를 따른 것뿐이다 |
| **스키마에 맞는 JSON 이 나온다** | **강제다.** 모델은 따르려 했는데 **나갈 수 없었다** |

강제와 순종이 **서로 반대 방향**을 가리키게 만든 것이 이 설계의 값이다. 결과가
어느 쪽이든 원인이 하나로 특정된다. ⛔ "스키마를 어겨 보라"로 짜지 않는다 —
모델의 순종도와 디코더의 강제가 섞여 정반대 결론이 나온다.

## 호출 건수가 1~2 건인 이유

`parse_into` 는 검증 실패 시 1회 재시도한다. 따라서

- **강제돼 있으면 1건** — 첫 호출이 스키마에 맞는 JSON 을 낸다
- **강제가 아니면 2건** — 산문이 와서 재시도가 붙는다

즉 **답이 "아니오"일 때만 2건**이 된다. 승인 상한 2건은 여기서 나온 수다.

## 실행

    python -m extraction.probe
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from collectors.rss import load_config
from extraction.llm import LLMError, client_from_config

#: 모델에게 **구조화 출력을 쓰지 말라고** 지시한다. 강제가 켜져 있으면 이 지시는
#: 지켜질 수 없다. 이 문구를 고치면 판별의 의미가 바뀌므로 함부로 고치지 않는다.
PROBE_SYSTEM = (
    "너는 한국어로 답하는 조수다. "
    "JSON 을 쓰지 마라. 중괄호를 쓰지 마라. 따옴표로 감싼 키-값 형식을 쓰지 마라. "
    "반드시 한국어 산문 두 문장으로만 답하라."
)

PROBE_USER = "물은 상온에서 액체인가? 두 문장으로 설명하라."


class ProbeAnswer(BaseModel):
    """판별용 최소 스키마. **enum 이라 디코더가 강제하면 다른 값이 나올 수 없다.**"""

    model_config = ConfigDict(populate_by_name=True)

    answer: Literal["예", "아니오"] = Field(alias="답")


@dataclass(frozen=True)
class ProbeResult:
    """판별 결과. `enforced` 가 결론이고 나머지는 그 근거다."""

    enforced: bool
    calls: int
    raw_contents: tuple[str, ...]
    finish_reasons: tuple[str | None, ...]
    model: str | None

    @property
    def verdict(self) -> str:
        return "강제됨" if self.enforced else "강제 아님"


def _contents(raws: list[dict[str, Any]]) -> tuple[tuple[str, ...], tuple[str | None, ...]]:
    contents: list[str] = []
    reasons: list[str | None] = []
    for raw in raws:
        choice = (raw.get("choices") or [{}])[0]
        contents.append((choice.get("message") or {}).get("content") or "")
        reasons.append(choice.get("finish_reason"))
    return tuple(contents), tuple(reasons)


def run_probe(client: Any) -> ProbeResult:
    """판별 1회. **호출 1~2건.**

    결과가 어느 쪽이든 예외로 올리지 않는다 — "강제가 아니다"는 오류가 아니라
    **판별 결과**이고, 그 결과를 들고 멈출지는 호출부가 정한다.
    """
    enforced = False
    try:
        client.parse_into(system=PROBE_SYSTEM, user=PROBE_USER, output_model=ProbeAnswer)
    except Exception:  # noqa: BLE001 — 검증 실패가 곧 "강제 아님"의 신호다
        enforced = False
    else:
        enforced = True

    raws = list(getattr(client, "last_raw_responses", []) or [])
    contents, reasons = _contents(raws)
    return ProbeResult(
        enforced=enforced,
        calls=len(raws),
        raw_contents=contents,
        finish_reasons=reasons,
        model=getattr(client, "model", None),
    )


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError):
            pass

    config = load_config()
    provider = ((config.get("llm") or {}).get("provider") or "").strip().lower()
    if provider != "vllm":
        # 이 probe 는 내부 엔드포인트의 성질을 재는 것이다. 벤더로 향한 채
        # 돌리면 재는 대상이 달라지고, 그건 게이트 1번의 문제이기도 하다.
        raise LLMError(
            f"llm.provider 가 {provider!r} 입니다. 이 probe 는 내부 vLLM 전용입니다"
        )

    client = client_from_config(config, stage="extraction")
    result = run_probe(client)

    print(
        json.dumps(
            {
                "verdict": result.verdict,
                "enforced": result.enforced,
                "calls": result.calls,
                "model": result.model,
                "finish_reasons": list(result.finish_reasons),
                "raw_contents": [c[:400] for c in result.raw_contents],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    if not result.enforced:
        print(
            "\n🔴 강제가 아니다. 이 상태에서 나온 스키마 실패율은 모델이 아니라"
            " 디코더를 잰다 (ADR-019). 측정을 진행하지 않는다.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
