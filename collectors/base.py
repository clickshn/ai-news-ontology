"""수집 레이어의 출력 계약.

`collectors/README.md` 의 "출력 계약" 절을 코드로 옮긴 것. 모든 Collector 는
`RawItem` 을 내놓고, extraction/ 은 `RawItem` 만 입력으로 받는다.

여기에는 요약·분류 결과가 들어가지 않는다. 원문 메타데이터만 담는다.
"""

from __future__ import annotations

from datetime import date
from typing import Iterable, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, HttpUrl


class RawItem(BaseModel):
    """수집된 원문 1건. 판단이 섞이지 않은 사실만."""

    model_config = ConfigDict(frozen=True)

    url: HttpUrl = Field(description="기사/논문 원문 URL. 중복 판정의 기준 키")
    title: str = Field(min_length=1, description="원문 제목 (그대로)")
    body: str = Field(
        default="",
        description="원문 본문 또는 피드가 제공한 요약/초록. HTML 태그는 제거된 평문",
    )
    source_name: str = Field(description="config.yaml 의 소스 이름. 예: 'Anthropic News'")
    published_at: date | None = Field(default=None, description="원문 발행일. 피드에 없으면 None")
    collected_at: date | None = Field(default=None, description="수집 시각(일 단위)")
    tags: tuple[str, ...] = Field(
        default=(), description="config.yaml 의 소스별 힌트 태그. 분류 결과가 아니다"
    )


@runtime_checkable
class Collector(Protocol):
    """모든 수집기의 최소 계약."""

    def fetch(self) -> Iterable[RawItem]:
        """원문을 가져온다. 개별 소스 실패는 삼키고 로그로 남긴다."""
        ...
