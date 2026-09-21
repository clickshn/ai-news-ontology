"""보존소의 실측 usage 를 소스별로 집계한다 (API 호출 없음).

## 왜 소스별인가

본문 길이가 소스마다 한 자릿수씩 다르다 — arXiv 초록 1,339자 대 Hugging Face Blog
0자(ADR-005). 입력 토큰이 그만큼 갈리므로 **전체 평균 단가로 2단계 220건을 추정하면
표본 구성이 바뀌는 순간 틀린다.** 2단계 표본은 1단계와 구성이 다르다.

단가는 `observability/README.md` 의 "게이트 비용 절감 실측" 과 같은 출처를 쓴다.
여기서 값을 새로 정하지 않는다 — 두 곳이 갈리면 어느 쪽이 맞는지 알 수 없다.

## 단가는 **단계가 아니라 모델**에 붙는다 (F4)

원래 이 파일은 `gate` 단계면 Haiku 단가, `extraction` 단계면 Opus 단가를 곱했다.
단계와 모델이 1:1 이던 동안에만 맞는 계산이고, **내부 vLLM 이전(ADR-018) 이후로는
틀린다** — 같은 두 단계가 `gemma-4-31B-it` 로 돌아가는데 벤더 달러가 계속 곱해져
**일어나지 않은 비용이 보고서에 찍힌다.** 그 수치가 2단계 220건 추정의 근거였다.

판정 기준을 레코드에 **보존된 모델명**으로 바꿨다 (`extraction/llm.py:
MODEL_UNSUPPORTED_PARAMS` 와 같은 기준 — 거부하는 주체도 곱해지는 주체도 모델이다).
"""

from __future__ import annotations

import argparse
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from export.store import DEFAULT_STORE_DIR, ExtractionStore

#: 모델명 접두사 → per 1M 토큰 단가. `observability/README.md` 와 같은 값.
#:
#: 접두사로 맞추는 이유는 `extraction/llm.py: MODEL_UNSUPPORTED_PARAMS` 와 같다 —
#: 날짜 접미사(`claude-haiku-4-5-20251001`)가 붙는 모델을 버전마다 등록하지 않는다.
#:
#: ⚠️ **`0.0` 과 미등록은 다르다.** `gemma-4-31B-it` 의 `0.0` 은 "우리가 토큰당
#: 청구를 받지 않는다"는 **등록된 사실**이고(내부 vLLM — 고정 인프라 비용은 여기서
#: 재는 대상이 아니다), 표에 없는 모델은 **모르는 것**이라 `None` 이 된다. 둘을 같은
#: 0 으로 적으면 "공짜로 돌았다"와 "단가를 모른다"가 한 칸에 섞인다.
MODEL_PRICES: tuple[tuple[str, dict[str, float]], ...] = (
    ("claude-opus-5", {"input": 5.0, "output": 25.0}),
    ("claude-haiku-4-5", {"input": 1.0, "output": 5.0}),
    ("gemma-4-31B-it", {"input": 0.0, "output": 0.0}),
)


def price_for(model: str | None) -> dict[str, float] | None:
    """`model` 의 per 1M 단가. **모르는 모델이면 `None`** — 0 이 아니다."""
    for prefix, price in MODEL_PRICES:
        if (model or "").startswith(prefix):
            return price
    return None


def _cost(tokens: dict[str | None, list[int]]) -> float | None:
    """모델별 토큰 → 비용. **한 모델이라도 단가를 모르면 합계가 `None` 이다.**

    모르는 모델의 몫만 빼고 더하면 그 합은 "이 소스의 비용"이 아니라 "이 소스에서
    우리가 아는 부분의 비용"인데, 이름이 같으면 둘이 구분되지 않는다.
    """
    total = 0.0
    for model, (inp, out) in tokens.items():
        price = price_for(model)
        if price is None:
            return None
        total += inp * price["input"] / 1e6 + out * price["output"] / 1e6
    return total


@dataclass
class SourceUsage:
    source_name: str
    records: int = 0
    gate_calls: int = 0
    thinking: int = 0
    body_chars: list[int] = field(default_factory=list)
    #: 모델명 → `[입력, 출력]` 토큰. **합쳐 두면 못 곱한다** — 한 소스 안에서도
    #: 실행 시점에 따라 모델이 갈린다(벤더 30건 + 이후 vLLM 실행).
    gate_tokens: dict[str | None, list[int]] = field(default_factory=dict)
    extract_tokens: dict[str | None, list[int]] = field(default_factory=dict)

    def add_gate(self, model: str | None, inp: int, out: int) -> None:
        self.gate_calls += 1
        bucket = self.gate_tokens.setdefault(model, [0, 0])
        bucket[0] += inp
        bucket[1] += out

    def add_extraction(self, model: str | None, inp: int, out: int) -> None:
        bucket = self.extract_tokens.setdefault(model, [0, 0])
        bucket[0] += inp
        bucket[1] += out

    @property
    def gate_input(self) -> int:
        return sum(t[0] for t in self.gate_tokens.values())

    @property
    def gate_output(self) -> int:
        return sum(t[1] for t in self.gate_tokens.values())

    @property
    def extract_input(self) -> int:
        return sum(t[0] for t in self.extract_tokens.values())

    @property
    def extract_output(self) -> int:
        return sum(t[1] for t in self.extract_tokens.values())

    @property
    def models(self) -> tuple[str | None, ...]:
        """이 소스에 실제로 쓰인 모델들. 게이트 · 추출을 합쳐 본다."""
        return tuple(sorted(
            set(self.gate_tokens) | set(self.extract_tokens),
            key=lambda m: (m is None, m or ""),
        ))

    @property
    def unpriced_models(self) -> tuple[str | None, ...]:
        """단가를 모르는 모델들. 비어 있지 않으면 이 소스의 비용은 `None` 이다."""
        return tuple(m for m in self.models if price_for(m) is None)

    @property
    def gate_cost(self) -> float | None:
        return _cost(self.gate_tokens)

    @property
    def extract_cost(self) -> float | None:
        return _cost(self.extract_tokens)

    @property
    def total_cost(self) -> float | None:
        gate, extract = self.gate_cost, self.extract_cost
        if gate is None or extract is None:
            return None
        return gate + extract

    @property
    def cost_per_record(self) -> float | None:
        """**재지 못한 건은 `None`.** 레코드가 0 건일 때 0.0 을 적으면 "돈이 안 들었다"로 읽힌다."""
        total = self.total_cost
        if total is None or not self.records:
            return None
        return total / self.records

    @property
    def median_body(self) -> int:
        if not self.body_chars:
            return 0
        ordered = sorted(self.body_chars)
        return ordered[len(ordered) // 2]


def collect_usage(store: ExtractionStore) -> dict[str, SourceUsage]:
    """보존소 전체를 소스명별로 집계한다.

    **게이트에서 스킵된 항목은 여기 없다.** 보존소는 추출까지 간 것만 담기 때문이다
    (스킵은 `observability/logs/skips.jsonl` 쪽 기록이다). 따라서 여기서 나오는
    게이트 호출 수는 실제 호출 수의 하한이다 — 2단계 추정에 쓸 때 이 점을 감안한다.
    """
    by_source: dict[str, SourceUsage] = defaultdict(lambda: SourceUsage(source_name=""))
    for stored in store:
        name = stored.raw_item["source_name"]
        usage = by_source[name]
        usage.source_name = name
        usage.records += 1
        usage.body_chars.append(len(stored.raw_item.get("body") or ""))

        extraction_usage = stored.extraction.get("usage") or {}
        usage.add_extraction(
            stored.extraction.get("model"),
            extraction_usage.get("input_tokens") or 0,
            extraction_usage.get("output_tokens") or 0,
        )
        usage.thinking += extraction_usage.get("thinking_tokens") or 0

        gate = stored.gate
        if gate:
            gate_usage = gate.get("usage") or {}
            # **게이트 모델은 추출 모델과 다를 수 있다** — 원래 다르라고 만든
            # 단계다(D-015: 싼 모델로 거른다). 단계 이름으로 단가를 고르면 이
            # 차이가 사라지고, 모델을 바꿔도 보고서가 안 움직인다.
            usage.add_gate(
                gate.get("model"),
                gate_usage.get("input_tokens") or 0,
                gate_usage.get("output_tokens") or 0,
            )
    return dict(by_source)


#: 단가를 모르는 칸에 찍는 표시. 빈칸이나 `$0.0000` 으로 두면 **0 으로 읽힌다.**
UNPRICED = "단가 미등록"


def format_report(by_source: dict[str, SourceUsage]) -> str:
    # 단가를 아는 소스를 비싼 순으로 먼저, 모르는 소스를 뒤에 이름순으로.
    rows = sorted(
        by_source.values(),
        key=lambda u: (u.cost_per_record is None, -(u.cost_per_record or 0.0), u.source_name),
    )
    lines = [
        "| 소스 | 건수 | 모델 | 본문 중앙값 | 추출 in/out | 게이트 in/out | 건당 비용 | 소계 |",
        "|---|---:|---|---:|---:|---:|---:|---:|",
    ]
    for u in rows:
        per_record = f"${u.cost_per_record:.4f}" if u.cost_per_record is not None else UNPRICED
        subtotal = f"${u.total_cost:.3f}" if u.total_cost is not None else UNPRICED
        models = ", ".join(m or "(모델명 없음)" for m in u.models) or "—"
        lines.append(
            f"| {u.source_name} | {u.records} | {models} | {u.median_body}자 | "
            f"{u.extract_input // max(u.records,1):,}/{u.extract_output // max(u.records,1):,} | "
            f"{u.gate_input // max(u.gate_calls,1):,}/{u.gate_output // max(u.gate_calls,1):,} | "
            f"{per_record} | {subtotal} |"
        )

    total_records = sum(u.records for u in rows)
    priced = [u for u in rows if u.total_cost is not None]
    priced_records = sum(u.records for u in priced)
    total_cost = sum(u.total_cost or 0.0 for u in priced)
    total_think = sum(u.thinking for u in rows)
    lines.append("")
    tail = f"thinking {total_think:,}토큰 — 출력에 포함된 부분집합이라 더하지 않는다"
    if priced_records:
        lines.append(
            f"합계 {total_records}건 / **${total_cost:.3f}** "
            f"(건당 평균 ${total_cost / priced_records:.4f}, {tail})"
        )
    else:
        # **`$0.000` 을 찍지 않는다.** 단가를 아는 건이 하나도 없을 때 0 을 적으면
        # 그 줄이 "공짜로 돌았다"가 된다 — 여기서 재지 못한 것은 비용 전부다.
        lines.append(f"합계 {total_records}건 / **{UNPRICED}** ({tail})")

    # **부분 합계를 합계로 적지 않는다.** 뺀 건수와 뺀 이유를 같은 줄에 붙인다.
    unpriced = [u for u in rows if u.total_cost is None]
    if unpriced:
        names = sorted({m or "(모델명 없음)" for u in unpriced for m in u.unpriced_models})
        lines.append(
            f"⚠️ 위 금액은 **단가를 아는 {priced_records}건만**이다. "
            f"{total_records - priced_records}건은 {UNPRICED} 모델이라 뺐다: {', '.join(names)}"
        )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="보존소 실측 usage 집계 (API 호출 없음)")
    parser.add_argument("--store-dir", default=str(DEFAULT_STORE_DIR))
    args = parser.parse_args(argv)

    store = ExtractionStore(Path(args.store_dir))
    by_source = collect_usage(store)
    if not by_source:
        print("보존소가 비어 있습니다.")
        return 1
    print(format_report(by_source))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
