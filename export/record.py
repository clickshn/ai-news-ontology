"""보존된 추출 1건 -> 계약 레코드 1줄 (계약 §3).

이 모듈은 **LLM 을 부르지 않는다.** 입력은 `export/store.py` 의 보존 결과뿐이다.
형식이 틀렸을 때 고쳐서 다시 돌리는 곳이 여기이므로, 여기에 API 호출이 섞이면
계약 §12.3 이 막으려던 "형식 결함에 LLM 비용을 또 내는 구조"가 되살아난다.
"""

from __future__ import annotations

from typing import Any

from export.contract import (
    CONTRACT_VERSION,
    TEXT_MAX_CHARS,
    TEXT_ORIGIN_SOURCE,
    compute_indexable,
)
from export.doc_id import canonical_url, doc_id_for, locator_for, source_for
from export.store import StoredExtraction

# 피드가 말줄임으로 잘라 보낸 흔적. `text_truncated` 는 "우리가 잘랐다"만이
# 아니라 "원문이 잘려서 왔다"도 포함한다 (계약 §3.1). GeekNews 50건 중 40건이
# 여기 해당한다 (D-014).
_ELLIPSIS_MARKS = ("…", "...", "‥", "···")


def declared_lang(tags: Any) -> str:
    """계약 §3.4. **소스 선언값이지 판정값이 아니다.**

    항목 단위 언어 판정을 하지 않는 이유는 "하면 좋은데 안 한 것"이 아니다.
    판정기를 넣는 순간 그 판정기가 MARA 크로스링구얼 실험의 숨은 변수가 된다.
    한국어 소스에 섞인 영어 항목이 `ko` 로 선언되는 오차는 계약이 해결하지 않고
    남겨 둔다.
    """
    return "ko" if "ko" in tuple(tags or ()) else "en"


def clamp_text(body: str) -> tuple[str, bool]:
    """계약 §6.1. 상한을 넘으면 자르고 잘렸다는 사실을 함께 돌려준다.

    Returns:
        (text, truncated)
    """
    body = body or ""
    stripped = body.rstrip()
    truncated = any(stripped.endswith(mark) for mark in _ELLIPSIS_MARKS)
    if len(body) > TEXT_MAX_CHARS:
        body = body[:TEXT_MAX_CHARS]
        truncated = True
    return body, truncated


def ontology_block(ontology: dict[str, Any]) -> dict[str, Any]:
    """계약 §3.2. 영문 필드명 + 파생 텍스트 2개를 최상위로 편다.

    한국어 alias 는 Obsidian frontmatter 용이라 계약에 싣지 않는다 — 기계가 읽는
    쪽에서 키가 한글일 이유가 없다. `impact` 중첩 객체도 `impact_score` /
    `impact_rationale` 두 키로 펴서 싣는다.
    """
    impact = ontology["impact"]
    return {
        "tech_domains": list(ontology["tech_domains"]),
        "release_type": ontology["release_type"],
        "companies": [
            {
                "canonical": company.get("canonical"),
                "raw": company["raw"],
                "resolved": bool(company.get("resolved", False)),
                "role": company.get("role"),
            }
            for company in ontology.get("companies", [])
        ],
        "prior_art": list(ontology.get("prior_art", [])),
        "impact_score": impact["score"],
        "impact_rationale": impact["rationale"],
        "summary": ontology["summary"],
    }


def provenance_block(stored: StoredExtraction, *, vocab_version: str) -> dict[str, Any]:
    """계약 §3.3. **선택 필드가 아니다.**

    온톨로지 5필드는 외부 벤더 모델과 특정 프롬프트 버전의 출력이다. 이 값이
    없으면 코퍼스가 "언제 어느 모델로 만들어졌는지 모르는 것"이 되고, MARA 의
    모델 고정 전제(ADR-002)가 코퍼스 쪽에서 조용히 깨진다. 그래서 MARA 는
    5키 중 하나라도 없으면 import 를 실패시킨다.
    """
    extraction = stored.extraction
    gate = stored.gate
    return {
        "extraction_model": extraction["model"],
        "prompt_version": extraction["prompt_name"],
        "prompt_sha256": extraction["prompt_sha256"],
        "extracted_at": extraction["extracted_at"],
        "gate_model": gate["model"] if gate else None,
        "gate_prompt_version": gate["prompt_name"] if gate else None,
        "vocab_version": vocab_version,
    }


def build_record(stored: StoredExtraction, *, vocab_version: str) -> dict[str, Any]:
    """보존된 추출 1건을 계약 레코드(JSONL 한 줄)로 만든다."""
    item = stored.raw_item
    url = item["url"]

    text, truncated = clamp_text(item.get("body", ""))
    text_chars = len(text)

    return {
        "contract_version": CONTRACT_VERSION,
        "doc_id": doc_id_for(url),
        "source": source_for(url),
        "source_name": item["source_name"],
        "url": canonical_url(url),
        "title": item["title"],
        "lang": declared_lang(item.get("tags")),
        "text": text,
        "text_origin": TEXT_ORIGIN_SOURCE,
        "text_chars": text_chars,
        "text_truncated": truncated,
        "locator": locator_for(url),
        # 누락은 빈 문자열이 아니라 null 이다 (계약 §3.1).
        "published_at": item.get("published_at") or None,
        "collected_at": item["collected_at"],
        "indexable": compute_indexable(TEXT_ORIGIN_SOURCE, text_chars),
        "ontology": ontology_block(stored.extraction["ontology"]),
        "provenance": provenance_block(stored, vocab_version=vocab_version),
    }
