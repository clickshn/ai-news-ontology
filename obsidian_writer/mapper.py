"""NewsOntology -> Obsidian 노트(frontmatter + 본문) 변환.

## 키 이름을 영문으로 쓰는 이유

`extraction/schema.py` 는 한국어 alias 를 쓰고(D-003), 여기서는 **영문 키**로
바꾼다. Dataview 쿼리(`WHERE impact_score > 3`)와 템플릿에서 한글 키는 따옴표로
감싸야 해서 매번 걸리적거리기 때문이다. 노트 안에서 사람이 읽는 것은 값이지 키가
아니므로, 키는 기계가 다루기 쉬운 쪽으로 맞춘다. (결정 로그 D-029)

변환은 `FRONTMATTER_KEYS` 하나로만 이뤄진다. 매핑을 코드 곳곳에 흩어 놓으면
스키마가 바뀔 때 어디를 고쳐야 하는지 알 수 없어진다.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import yaml

from collectors.base import RawItem
from extraction.schema import NewsOntology

# 한국어 alias -> frontmatter 영문 키. **이 표가 유일한 매핑 출처다.**
# 값이 None 인 항목은 frontmatter 에 싣지 않는다(구조가 평평하지 않아 별도 처리).
FRONTMATTER_KEYS: dict[str, str] = {
    "요약": "summary",            # 본문 "## 요약" 으로 가므로 frontmatter 에는 없음
    "기술영역": "tech_domain",
    "발표유형": "release_type",
    "관련기업": "companies",
    "관련기존기술": "prior_art",
    "영향도": "impact",           # score/rationale 두 키로 펴진다 (D-007)
}

# `영향도` 를 중첩 객체가 아니라 평평한 두 키로 펴는 이유는 D-007 참고.
IMPACT_KEYS = {"score": "impact_score", "rationale": "impact_rationale"}

# frontmatter 키 순서. dict 순서가 그대로 YAML 순서가 된다.
FRONTMATTER_ORDER = [
    "title",
    "date",
    "source",
    "source_url",
    "tech_domain",
    "release_type",
    "companies",
    "prior_art",
    "impact_score",
    "impact_rationale",
    "extraction_model",
    "prompt_version",
    "processed_at",
]


@dataclass(frozen=True)
class NoteContext:
    """노트 1개를 만드는 데 필요한, 온톨로지 밖의 정보."""

    item: RawItem
    extraction_model: str | None = None
    prompt_version: str | None = None
    processed_at: datetime | None = None

    def resolved_processed_at(self) -> datetime:
        return self.processed_at or datetime.now(timezone.utc).astimezone()


def build_frontmatter(ontology: NewsOntology, context: NoteContext) -> dict[str, Any]:
    """frontmatter dict 를 만든다. 키 순서는 `FRONTMATTER_ORDER` 를 따른다.

    `관련기업` 은 **canonical(정규화된 대표명)** 을 싣는다. 원문표기를 쓰면 같은
    기업이 표기마다 다른 값으로 갈려 집계가 무의미해진다. 원문표기는 노트에
    남기지 않는다 — 필요하면 `extraction` 단계 로그에서 찾는다. (D-030)
    """
    processed_at = context.resolved_processed_at()
    impact = ontology.impact

    data: dict[str, Any] = {
        "title": context.item.title,
        "date": processed_at.date().isoformat(),
        "source": context.item.source_name,
        "source_url": str(context.item.url),
        "tech_domain": [d.value for d in ontology.tech_domains],
        "release_type": ontology.release_type.value,
        "companies": [c.canonical for c in ontology.companies if c.canonical],
        "prior_art": list(ontology.prior_art),
        IMPACT_KEYS["score"]: impact.score,
        IMPACT_KEYS["rationale"]: impact.rationale,
        "extraction_model": context.extraction_model,
        "prompt_version": context.prompt_version,
        "processed_at": processed_at.isoformat(),
    }
    return {key: data[key] for key in FRONTMATTER_ORDER if key in data}


def dump_frontmatter(data: dict[str, Any]) -> str:
    """frontmatter 를 YAML 로 직렬화한다.

    `allow_unicode=True` 가 없으면 한글이 `\\uXXXX` 로 이스케이프돼 Obsidian 에서
    읽을 수 없는 문자열이 된다. `sort_keys=False` 로 `FRONTMATTER_ORDER` 를 지킨다.
    """
    return yaml.safe_dump(
        data,
        allow_unicode=True,
        sort_keys=False,
        default_flow_style=False,
        width=10_000,   # 긴 근거 문장이 줄바꿈으로 접히지 않게
    )


#: 위키링크 **안에서 구분자로 읽히는** 문자 → 대체할 문자열.
#:
#: `|` 는 별칭 구분자(`[[대상|보이는 글자]]`), `#` 은 제목 구분자
#: (`[[대상#소제목]]`)다. 둘 다 링크를 깨지 않고 **다른 대상을 가리키게** 만든다
#: — `[[C#]]` 은 깨진 링크가 아니라 `C` 노트의 빈 제목으로 가는 멀쩡한 링크다.
#: 그래서 오류가 아니라 **조용히 틀린 그래프**가 남는다 (D-030 이 막으려던 것과
#: 같은 종류의 분기다).
_LINK_DELIMITERS = {"|": " ", "#": " "}


def _wikilink(value: str) -> str:
    """Obsidian 위키링크. 링크 문법을 깨거나 **대상을 바꾸는** 문자를 지운다.

    ⚠️ 한계: Obsidian 위키링크 대상에는 `#` 를 담을 방법이 없다(이스케이프
    문법이 없다). 그래서 `C#` 같은 이름은 `C` 가 되어 다른 노드와 합쳐진다.
    담을 방법이 없는 것을 담은 척하는 것보다 낫다고 보고 이쪽을 골랐다 —
    되돌리려면 위키링크가 아니라 마크다운 링크(`[C#](C%23.md)`)로 바꿔야 하고,
    그건 `관련 개념` 블록 전체의 표현을 바꾸는 일이다.
    """
    cleaned = value.replace("[[", "").replace("]]", "")
    for char, replacement in _LINK_DELIMITERS.items():
        cleaned = cleaned.replace(char, replacement)
    return f"[[{cleaned.strip()}]]"


def render_body(ontology: NewsOntology, context: NoteContext) -> str:
    """노트 본문(frontmatter 아래)을 만든다.

    `관련 개념` 에는 `관련기존기술` 과 `관련기업` 을 모두 위키링크로 넣는다.
    기업을 링크하면 "이 기업이 언급된 노트" 그래프가 공짜로 생긴다. 링크 대상은
    반드시 canonical 이다 — 원문표기로 링크하면 `오픈AI` 와 `OpenAI` 가 서로 다른
    노드가 되어 정규화(D-025)를 해 놓고 그래프에서 도로 갈라진다. (D-030)
    """
    lines: list[str] = ["## 요약", "", ontology.summary.strip(), ""]

    concepts = [_wikilink(tag) for tag in ontology.prior_art]
    companies = [_wikilink(c.canonical) for c in ontology.companies if c.canonical]

    lines.append("## 관련 개념")
    lines.append("")
    if concepts or companies:
        lines.extend(f"- {link}" for link in concepts + companies)
    else:
        lines.append("- (없음)")
    lines.append("")

    lines.append("## 원문")
    lines.append("")
    lines.append(f"[{context.item.source_name}에서 보기]({context.item.url})")
    lines.append("")
    return "\n".join(lines)


def render_note(ontology: NewsOntology, context: NoteContext) -> str:
    """frontmatter + 본문 전체 문자열."""
    frontmatter = dump_frontmatter(build_frontmatter(ontology, context))
    return f"---\n{frontmatter}---\n\n{render_body(ontology, context)}"
