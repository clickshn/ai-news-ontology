"""기업/기관명 정규화.

`config.yaml` 의 `company_aliases` 를 읽어 원문 표기를 대표명으로 옮긴다.
D-011 에서 설계했지만 구현이 비어 있던 단계다.

## 이 모듈이 지키는 것

**퍼지 매칭을 하지 않는다.** 문자열 유사도나 부분 문자열로 추측 매칭하지 않고,
사전에 명시적으로 등록된 표기만 정규화한다. 근거는 오류의 비대칭성이다
(결정 로그 D-025):

  - 정규화를 **놓치면** `정규화성공=false` 로 남고 원문 표기가 보존된다.
    나중에 사전에 표기를 추가하면 복구된다. 손실은 "그래프 노드가 하나 더 생김".
  - 정규화를 **잘못하면** 서로 다른 기업이 같은 노드로 합쳐진다. Obsidian
    그래프에 없는 관계가 생기고, 합쳐진 뒤에는 원래 무엇이었는지 알 수 없다.

즉 미탐은 되돌릴 수 있고 오탐은 되돌릴 수 없다. 그래서 정밀도를 재현율보다
우선한다 (관련성 게이트가 재현율을 우선하는 것과 정반대의 이유로).

흡수하는 차이는 **표기 노이즈**뿐이다: 대소문자, 공백/구분자.
`Open AI` → `OpenAI` 는 흡수하지만, `OpenAI Korea` 는 별개 엔티티로 남긴다.

## 순환 참조 주의

`extraction/schema.py` 가 이 모듈을 import 한다. 따라서 여기서는 schema 를
import 하지 않고 **문자열만 다룬다.**
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Mapping

import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config.yaml"

# 대소문자와 공백류(스페이스/탭/개행/비파괴 공백)만 흡수한다.
# 하이픈·점·& 같은 문자는 남긴다 — 'M&A' 처럼 의미가 있는 경우가 있고,
# 지우기 시작하면 어디까지 지울지의 기준이 사라진다.
_WS_RE = re.compile(r"[\s ]+")


@dataclass(frozen=True)
class NormalizedCompany:
    """정규화 결과."""

    canonical: str
    resolved: bool


def normalization_key(name: str) -> str:
    """매칭용 키. 대소문자와 공백 차이만 흡수한다."""
    return _WS_RE.sub("", name).casefold()


def build_alias_index(company_aliases: Mapping[str, list[str]]) -> dict[str, str]:
    """`{대표명: [표기, ...]}` 를 `{정규화키: 대표명}` 으로 뒤집는다.

    대표명 자체도 키로 넣는다. 사전에 별칭만 적고 대표명을 빼먹어도
    `OpenAI` 라는 원문 표기가 정규화되도록 하기 위함이다.

    같은 키가 서로 다른 대표명으로 두 번 등록되면 사전 오류다. 조용히 덮어쓰면
    어느 쪽이 이겼는지 알 수 없으므로 경고를 남기고 **먼저 등록된 쪽을 유지**한다.
    """
    index: dict[str, str] = {}

    def put(key_source: str, canonical: str) -> None:
        key = normalization_key(key_source)
        if not key:
            return
        existing = index.get(key)
        if existing is not None and existing != canonical:
            print(
                f"[warn] company_aliases 충돌: {key_source!r} 가 "
                f"{existing!r} 와 {canonical!r} 양쪽에 등록돼 있습니다. "
                f"{existing!r} 를 유지합니다.",
                file=sys.stderr,
            )
            return
        index[key] = canonical

    for canonical, aliases in (company_aliases or {}).items():
        put(canonical, canonical)
        for alias in aliases or []:
            put(alias, canonical)

    return index


@lru_cache(maxsize=4)
def load_alias_index(config_path: Path | str = DEFAULT_CONFIG_PATH) -> dict[str, str]:
    """config.yaml 에서 alias 인덱스를 만든다. 결과는 캐시된다.

    설정을 읽지 못해도 **예외를 올리지 않는다.** 정규화는 파이프라인의 부가
    단계이고, 사전이 없으면 전부 미정규화(원문 승계)로 동작하는 것이 안전한
    기본값이다. 이 함수는 pydantic 검증 경로에서 호출되므로, 여기서 던지면
    설정 파일 문제가 스키마 검증 실패로 둔갑한다.
    """
    try:
        with open(config_path, encoding="utf-8") as f:
            config = yaml.safe_load(f) or {}
    except (OSError, yaml.YAMLError) as exc:
        print(
            f"[warn] company_aliases 로드 실패 ({type(exc).__name__}: {exc}). "
            "정규화 없이 원문 표기를 그대로 씁니다.",
            file=sys.stderr,
        )
        return {}

    return build_alias_index(config.get("company_aliases") or {})


def normalize_company(
    raw: str,
    *,
    index: Mapping[str, str] | None = None,
) -> NormalizedCompany:
    """원문 표기를 대표명으로 정규화한다.

    Args:
        raw: 본문에 나타난 그대로의 표기.
        index: 정규화키 -> 대표명. None 이면 config.yaml 에서 읽은 캐시본을 쓴다.

    Returns:
        매칭되면 `(대표명, True)`, 아니면 `(원문 표기 그대로, False)`.

    사전에 없는 표기는 **추측하지 않는다.** 원문을 그대로 승계하고
    `resolved=False` 로 남겨, 나중에 사전을 보강할 대상으로 관측되게 한다.
    """
    if not raw:
        return NormalizedCompany(canonical=raw, resolved=False)

    lookup = load_alias_index() if index is None else index
    canonical = lookup.get(normalization_key(raw))
    if canonical is None:
        return NormalizedCompany(canonical=raw, resolved=False)
    return NormalizedCompany(canonical=canonical, resolved=True)


def reset_cache() -> None:
    """설정을 바꾼 뒤 캐시를 비운다 (테스트·장기 실행 프로세스용)."""
    load_alias_index.cache_clear()
