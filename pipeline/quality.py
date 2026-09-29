"""성공한 실행의 품질 지표 — **기록만 한다. 판정하지 않는다** (ADR-025).

## 왜 기록만 하나

새 소스의 정상 범위를 아직 모른다. 임계값은 기록 2주 뒤에 정한다. 지금 기록하는 이유는
기준선이다 — 이력이 없으면 2주 뒤에도 정할 근거가 없다.

## 이 지표가 잡지 못하는 것

**근거 없는 채움과 분류 오류.** session-16 의 인공지능신문 항목(D-107)은 필드가 차
있었고 값도 어휘 안이었다 — 분포로 보면 "잘 채워진" 쪽이다. 그것을 드러낸 것은 사람의
원문 대조였다. 그래서 사람 경로는 상시 승인 갱신의 표본 대조로 따로 있다
(`pipeline.approval.SAMPLE_SIZE`).

본문 길이 중앙값이 첫 줄인 이유: 그때 문제를 부른 것이 출력이 아니라 **입력**(300자
잘림)이었다. 피드가 조용히 본문을 자르기 시작하면 여기서 먼저 보인다.

`stop_kinds`(절단·퇴화, D-093)는 파이프라인 보존소에 원본 응답이 남지 않아 여기서
세지 못한다 — 스키마 재시도율로 대신한다.
"""

from __future__ import annotations

from collections import Counter
from typing import Any

from export.store import ExtractionStore


def _rate(num: int, den: int) -> float | None:
    return round(num / den, 3) if den else None


def quality_metrics(report: dict[str, Any], store: ExtractionStore) -> dict[str, Any]:
    """소스별 지표. 보존소에서 이 실행이 보존한 항목만 읽는다. LLM 호출 없음."""
    out: dict[str, Any] = {}
    for name, tally in (report.get("sources") or {}).items():
        inputs = (report.get("inputs") or {}).get(name) or {}
        ids = (report.get("extracted_ids") or {}).get(name) or []
        release = Counter()
        impact = Counter()
        retried = prior_art_empty = companies = unresolved = 0
        for doc_id in ids:
            try:
                extraction = store.load(doc_id).extraction
            except Exception:
                continue
            ontology = extraction.get("ontology") or {}
            if int(extraction.get("attempts") or 1) > 1:
                retried += 1
            release[ontology.get("release_type")] += 1
            impact[str((ontology.get("impact") or {}).get("score"))] += 1
            if not ontology.get("prior_art"):
                prior_art_empty += 1
            for company in ontology.get("companies") or []:
                companies += 1
                if not company.get("resolved"):
                    unresolved += 1
        judged = tally.get("gate_passed", 0) + tally.get("gate_skipped_now", 0)
        calls = tally.get("extract_calls", 0)
        out[name] = {
            "body_chars_median": inputs.get("body_chars_median"),
            "body_empty": inputs.get("body_empty"),
            "gate_pass_rate": _rate(tally.get("gate_passed", 0), judged),
            "schema_retry_rate": _rate(retried + tally.get("extract_schema_failed", 0), calls),
            "extracted": len(ids),
            "unresolved_company_rate": _rate(unresolved, companies),
            "prior_art_empty_rate": _rate(prior_art_empty, len(ids)),
            "release_type": dict(release),
            "impact": dict(sorted(impact.items())),
        }
    return out
