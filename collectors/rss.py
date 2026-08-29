"""RSS/Atom 수집기.

config.yaml 의 `sources.rss` 목록을 feedparser 로 파싱해 `RawItem` 으로 변환한다.

이 모듈은 **판단하지 않는다.** 요약도 분류도 하지 않고, 피드가 준 텍스트에서
HTML 태그만 벗겨 그대로 넘긴다. (README 결정 로그 D-001 / collectors/README.md)

CLI 확인용:
    python -m collectors.rss --limit 5
    python -m collectors.rss --source "Anthropic News" --limit 3 --json
"""

from __future__ import annotations

import argparse
import html
import json
import re
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterator

import feedparser
import yaml
from pydantic import ValidationError

from collectors.base import RawItem

DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent.parent / "config.yaml"

# 피드 본문은 길이가 제각각이다. LLM 입력 비용을 예측 가능하게 두려고 여기서 자른다.
# 잘린 사실은 extractor 프롬프트에서 "본문이 잘렸을 수 있다"로 명시한다.
BODY_MAX_CHARS = 4000

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"[ \t ]+")
_BLANKLINE_RE = re.compile(r"\n{3,}")


# ---------------------------------------------------------------------------
# 설정
# ---------------------------------------------------------------------------
def load_config(path: Path | str = DEFAULT_CONFIG_PATH) -> dict[str, Any]:
    """config.yaml 을 읽는다. 없거나 깨졌으면 그대로 예외를 올린다.

    설정 파일 문제는 조용히 넘어가면 안 되는 종류의 실패다 — 피드 1개 실패와 달리
    전체 실행의 전제가 무너진 상황이므로 여기서는 방어하지 않는다.
    """
    with open(path, encoding="utf-8") as f:
        config = yaml.safe_load(f)
    if not isinstance(config, dict):
        raise ValueError(f"config.yaml 의 최상위가 매핑이 아닙니다: {path}")
    return config


def rss_sources(config: dict[str, Any]) -> list[dict[str, Any]]:
    """config 에서 RSS 소스 목록을 꺼낸다."""
    sources = (config.get("sources") or {}).get("rss") or []
    if not isinstance(sources, list):
        raise ValueError("config.yaml: sources.rss 는 리스트여야 합니다")
    return sources


# ---------------------------------------------------------------------------
# 정리 유틸
# ---------------------------------------------------------------------------
def strip_html(raw: str) -> str:
    """피드 본문에서 태그를 벗기고 공백을 정리한다.

    완전한 HTML 파서가 아니다. 피드 요약문 수준의 단순 마크업만 다루면 되므로
    의존성을 늘리지 않고 정규식으로 처리한다. 태그 안에 '>' 가 들어간 병리적
    입력은 정확히 처리되지 않지만, 그 경우에도 텍스트가 남으므로 치명적이지 않다.
    """
    if not raw:
        return ""
    text = _TAG_RE.sub(" ", raw)
    text = html.unescape(text)
    text = _WS_RE.sub(" ", text)
    text = _BLANKLINE_RE.sub("\n\n", text)
    return text.strip()


def _entry_body(entry: Any) -> str:
    """엔트리에서 가장 정보량이 많은 텍스트를 고른다.

    content(전문) > summary(요약) 순. arXiv 는 summary 에 초록을 담고,
    블로그 피드는 content 에 전문을 담는 경우가 많다.
    """
    candidates: list[str] = []
    for block in getattr(entry, "content", None) or []:
        value = block.get("value") if isinstance(block, dict) else None
        if value:
            candidates.append(value)
    for key in ("summary", "description"):
        value = entry.get(key) if hasattr(entry, "get") else None
        if value:
            candidates.append(value)

    if not candidates:
        return ""
    best = max((strip_html(c) for c in candidates), key=len)
    return best[:BODY_MAX_CHARS]


def _entry_date(entry: Any) -> date | None:
    """발행일 파싱. 피드마다 필드명이 달라 후보를 순회한다."""
    for key in ("published_parsed", "updated_parsed", "created_parsed"):
        parsed = getattr(entry, key, None)
        if parsed:
            try:
                return datetime(*parsed[:6], tzinfo=timezone.utc).date()
            except (TypeError, ValueError):
                continue
    return None


# ---------------------------------------------------------------------------
# 수집
# ---------------------------------------------------------------------------
def fetch_source(source: dict[str, Any], *, limit: int | None = None) -> list[RawItem]:
    """피드 1개를 수집한다.

    예외 처리 방침(이번 단계):
      - 네트워크/파싱 실패는 여기서 흡수하고 빈 리스트를 반환한다. 피드 1개가
        전체 실행을 죽이면 안 된다.
      - 재시도는 넣지 않는다. 어떤 실패가 실제로 잦은지 모르는 상태에서 만든
        재시도 정책은 대개 틀린다. 실패 양상을 관찰한 뒤 붙인다.
      - 개별 엔트리의 검증 실패(ValidationError)는 그 엔트리만 건너뛴다.
    """
    name = source.get("name") or source.get("url", "<unnamed>")
    url = source.get("url")
    if not url:
        print(f"[skip] {name}: url 이 없습니다", file=sys.stderr)
        return []

    try:
        feed = feedparser.parse(url)
    except Exception as exc:  # feedparser 는 대개 삼키지만 방어적으로 잡는다
        print(f"[fail] {name}: 피드 요청 실패 ({type(exc).__name__}: {exc})", file=sys.stderr)
        return []

    status = getattr(feed, "status", None)
    if status is not None and status >= 400:
        print(f"[fail] {name}: HTTP {status}", file=sys.stderr)
        return []

    if getattr(feed, "bozo", False) and not feed.entries:
        reason = getattr(feed, "bozo_exception", "unknown")
        print(f"[fail] {name}: 파싱 실패 ({reason})", file=sys.stderr)
        return []

    entries = feed.entries[:limit] if limit else feed.entries
    tags = tuple(source.get("tags") or ())
    today = date.today()

    items: list[RawItem] = []
    for entry in entries:
        link = entry.get("link")
        title = (entry.get("title") or "").strip()
        if not link or not title:
            continue
        try:
            items.append(
                RawItem(
                    url=link,
                    title=title,
                    body=_entry_body(entry),
                    source_name=name,
                    published_at=_entry_date(entry),
                    collected_at=today,
                    tags=tags,
                )
            )
        except ValidationError as exc:
            print(f"[skip] {name}: 엔트리 검증 실패 {link} ({exc.error_count()}건)", file=sys.stderr)
            continue

    return items


def collect(
    config: dict[str, Any] | None = None,
    *,
    source_name: str | None = None,
    limit_per_source: int | None = None,
) -> Iterator[RawItem]:
    """설정의 모든 RSS 소스를 순회하며 RawItem 을 흘려보낸다.

    Args:
        config: 미리 읽은 설정. None 이면 기본 경로에서 읽는다.
        source_name: 지정하면 해당 이름의 소스만 수집한다.
        limit_per_source: 소스당 최대 건수.
    """
    config = config if config is not None else load_config()
    for source in rss_sources(config):
        if source_name and source.get("name") != source_name:
            continue
        yield from fetch_source(source, limit=limit_per_source)


# ---------------------------------------------------------------------------
# CLI (동작 확인용)
# ---------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="RSS 수집 결과를 표준출력에 찍는다 (확인용)")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG_PATH), help="config.yaml 경로")
    parser.add_argument("--source", default=None, help="소스 이름으로 필터")
    parser.add_argument("--limit", type=int, default=5, help="소스당 최대 건수")
    parser.add_argument("--json", action="store_true", help="JSON Lines 로 출력")
    args = parser.parse_args(argv)

    config = load_config(args.config)
    items = list(collect(config, source_name=args.source, limit_per_source=args.limit))

    for item in items:
        if args.json:
            print(json.dumps(item.model_dump(mode="json"), ensure_ascii=False))
        else:
            preview = item.body[:160].replace("\n", " ")
            print(f"[{item.source_name}] {item.published_at} {item.title}")
            print(f"  {item.url}")
            print(f"  {preview}{'...' if len(item.body) > 160 else ''}\n")

    print(f"총 {len(items)}건", file=sys.stderr)
    return 0 if items else 1


if __name__ == "__main__":
    raise SystemExit(main())
