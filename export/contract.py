"""출력 계약 v1 의 상수와 어휘 스냅샷.

계약 전문은 MARA 레포 `docs/contracts/ai-news-ontology-export-v1.md` 에 있다.
여기 있는 값들은 그 문서를 **참조하는 것이 아니라 복사해 고정한 것**이다.
참조였다면 남의 레포의 상수 변경이 우리 출력 형식을 조용히 바꾼다.

## `TEXT_MAX_CHARS` 가 `collectors.rss.BODY_MAX_CHARS` 와 별개인 이유

두 값은 지금 똑같이 4,000 이지만 **같은 상수가 아니다** (계약 §6.1).
수집 쪽 상한은 "LLM 입력 비용을 예측 가능하게 두려고" 있는 우리 사정이고,
계약 상한은 MARA 가 import 에서 강제하는 규칙이다. 수집 상한을 올리더라도
계약 상한은 4,000 이고, 초과분은 export 가 자른다.
"""

from __future__ import annotations

import hashlib

from extraction.schema import ReleaseType, TechDomain

# 계약 §3.1. major 가 다르면 MARA 가 거부한다.
CONTRACT_VERSION = "1.0"

# 계약 §6.1. **계약 상수다.** 바꾸면 이미 인덱싱된 문서의 text 가 달라져
# 재인덱싱이 필요하고 contract_version major 인상 사안이 된다 (§9).
TEXT_MAX_CHARS = 4000

# 계약 §6. v1 은 "비어 있지 않음". 더 높은 값이 옳을 수 있지만 근거 없이 정한
# 임계값을 또 만들지 않는다 — 전 레코드의 text_chars 분포를 보고 측정으로 정한다.
MIN_TEXT_CHARS = 1

# 계약 §6. v1 에서 유효한 유일한 값. `summary`/`impact_rationale` 같은 파생
# 텍스트를 색인하려면 값을 추가하는 것이 아니라 ADR 이 필요하다.
TEXT_ORIGIN_SOURCE = "source_text"

LICENSE_NOTE = (
    "공개 RSS/Atom 피드가 제공한 메타데이터와 발췌만 저장한다. 기사·논문 전문은 보관하지 않는다."
)


def tech_domain_vocab() -> list[str]:
    """통제어휘 스냅샷 — 정본은 `extraction/schema.py` 의 Enum 이다 (D-002)."""
    return [m.value for m in TechDomain]


def release_type_vocab() -> list[str]:
    return [m.value for m in ReleaseType]


def schema_sha256() -> str:
    """Enum 어휘의 해시. 어휘가 바뀌면 값이 바뀐다.

    직렬화 형식을 여기 고정한다 — 형식이 흔들리면 어휘가 그대로인데도 해시가
    달라져서 MARA 가 "어휘가 바뀌었다"고 잘못 읽는다. 순서는 Enum 선언 순서를
    따른다(정렬하지 않는다). 값의 **집합**뿐 아니라 순서 변경도 사람이 들여다볼
    만한 사건이기 때문이다.
    """
    payload = (
        "tech_domain=" + ",".join(tech_domain_vocab()) + ";"
        "release_type=" + ",".join(release_type_vocab())
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def vocab_version(config_version: object) -> str:
    """계약 §5 의 `vocab_version`. 예: `config=1;schema_sha256=9ff5...`."""
    return f"config={config_version};schema_sha256={schema_sha256()}"


def vocab_snapshot(config_version: object) -> dict[str, object]:
    """manifest 에 싣는 어휘 스냅샷 (계약 §5).

    MARA 는 이 목록을 코드에 복사하지 않고 **검증용으로만** 쓴다. 값 추가는
    통과, 삭제·개명·쪼개짐은 import 실패다 — 이미 인덱싱된 문서의 라벨이
    소급해서 틀린 것이 되기 때문이다 (생산자 D-021/D-022).
    """
    return {
        "vocab_version": vocab_version(config_version),
        "tech_domain": tech_domain_vocab(),
        "release_type": release_type_vocab(),
    }


def compute_indexable(text_origin: str, text_chars: int) -> bool:
    """계약 §6 의 색인 규칙.

    MARA 는 이 값을 믿지 않고 **재계산해서 대조한다.** 그래서 이 함수는 한 줄
    짜리지만 export 와 자체 검증기가 **같은 함수를 쓰게** 묶어 둔다 — 두 곳에
    같은 식을 손으로 적으면 대조가 자기 자신과의 대조가 된다.
    """
    return text_origin == TEXT_ORIGIN_SOURCE and text_chars >= MIN_TEXT_CHARS
