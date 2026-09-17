"""보존소의 실측 usage 를 소스별로 집계한다 (API 호출 없음).

## 왜 소스별인가

본문 길이가 소스마다 한 자릿수씩 다르다 — arXiv 초록 1,339자 대 Hugging Face Blog
0자(ADR-005). 입력 토큰이 그만큼 갈리므로 **전체 평균 단가로 2단계 220건을 추정하면
표본 구성이 바뀌는 순간 틀린다.** 2단계 표본은 1단계와 구성이 다르다.

단가는 `observability/README.md` 의 "게이트 비용 절감 실측" 과 같은 출처를 쓴다.
여기서 값을 새로 정하지 않는다 — 두 곳이 갈리면 어느 쪽이 맞는지 알 수 없다.
"""

from __future__ import annotations

import argparse
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from export.store import DEFAULT_STORE_DIR, ExtractionStore

# per 1M tokens. observability/README.md 와 같은 값.
PRICES = {
    "gate": {"input": 1.0, "output": 5.0},        # Haiku 4.5
    "extraction": {"input": 5.0, "output": 25.0},  # Opus 5
}


@dataclass
class SourceUsage:
    source_name: str
    records: int = 0
    gate_calls: int = 0
    gate_input: int = 0
    gate_output: int = 0
    extract_input: int = 0
    extract_output: int = 0
    thinking: int = 0
    body_chars: list[int] = field(default_factory=list)

    @property
    def gate_cost(self) -> float:
        p = PRICES["gate"]
        return self.gate_input * p["input"] / 1e6 + self.gate_output * p["output"] / 1e6

    @property
    def extract_cost(self) -> float:
        p = PRICES["extraction"]
        return self.extract_input * p["input"] / 1e6 + self.extract_output * p["output"] / 1e6

    @property
    def total_cost(self) -> float:
        return self.gate_cost + self.extract_cost

    @property
    def cost_per_record(self) -> float:
        return self.total_cost / self.records if self.records else 0.0

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

        extraction = stored.extraction.get("usage") or {}
        usage.extract_input += extraction.get("input_tokens") or 0
        usage.extract_output += extraction.get("output_tokens") or 0
        usage.thinking += extraction.get("thinking_tokens") or 0

        gate = stored.gate
        if gate:
            gate_usage = gate.get("usage") or {}
            usage.gate_calls += 1
            usage.gate_input += gate_usage.get("input_tokens") or 0
            usage.gate_output += gate_usage.get("output_tokens") or 0
    return dict(by_source)


def format_report(by_source: dict[str, SourceUsage]) -> str:
    rows = sorted(by_source.values(), key=lambda u: -u.cost_per_record)
    lines = [
        "| 소스 | 건수 | 본문 중앙값 | 추출 in/out | 게이트 in/out | 건당 비용 | 소계 |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for u in rows:
        lines.append(
            f"| {u.source_name} | {u.records} | {u.median_body}자 | "
            f"{u.extract_input // max(u.records,1):,}/{u.extract_output // max(u.records,1):,} | "
            f"{u.gate_input // max(u.gate_calls,1):,}/{u.gate_output // max(u.gate_calls,1):,} | "
            f"${u.cost_per_record:.4f} | ${u.total_cost:.3f} |"
        )
    total_records = sum(u.records for u in rows)
    total_cost = sum(u.total_cost for u in rows)
    total_think = sum(u.thinking for u in rows)
    lines.append("")
    lines.append(
        f"합계 {total_records}건 / **${total_cost:.3f}** "
        f"(건당 평균 ${total_cost / total_records if total_records else 0:.4f}, "
        f"thinking {total_think:,}토큰 — 출력에 포함된 부분집합이라 더하지 않는다)"
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
